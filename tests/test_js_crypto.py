"""
JS CryptoProvider 单元测试

使用 Playwright 在真实浏览器中测试 crypto_providers.js 的加密提供者。
参考 test_mobile.py 的模式：page.set_content + page.evaluate。

新架构（e2ee-always-on-design.md）：
- 加密永远开启，不存在明文模式
- 认证方式：URL fragment（扫码）或 TOFU（手动审批）

测试覆盖：
- NaClBoxProvider: XSalsa20-Poly1305 加解密往返、auth 握手
- XChaCha20Provider: XChaCha20-Poly1305 AEAD 加解密往返、auth 握手
- TOFU 辅助函数: unsealTofuChallenge 与 Python 端互操作
- 跨平台互操作: JS ↔ Python（PyNaCl 模拟 PC 端）
- 跨算法隔离: 不同算法之间无法互通
"""

import base64
import json
from hashlib import blake2b
from pathlib import Path

import pytest
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import Aead, SecretBox
from nacl.bindings import crypto_scalarmult
from nacl.utils import random as random_bytes

from phonemic.tunnel.crypto import create_provider, encode_mac, upload_mac
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

pytest.importorskip("playwright")

RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"
SODIUM_JS = (RES_DIR / "sodium.js").read_text(encoding="utf-8")
# 跨平台握手用例在 JS 侧要用 MessagePack 解析解密后的帧
MSGPACK_JS = (RES_DIR / "msgpack.min.js").read_text(encoding="utf-8")
CRYPTO_JS = (RES_DIR / "crypto_providers.js").read_text(encoding="utf-8")

TEST_HTML = (
    "<!DOCTYPE html><html><head>"
    f"<script>{SODIUM_JS}</script>"
    f"<script>{MSGPACK_JS}</script>"
    f"<script>{CRYPTO_JS}</script>"
    "</head><body></body></html>"
)


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


@pytest.fixture
def crypto_page(page):
    """加载包含 sodium + crypto_providers 的页面。"""
    page.set_content(TEST_HTML)
    page.wait_for_function(
        "() => typeof sodium !== 'undefined' && typeof NaClBoxProvider !== 'undefined'"
    )
    yield page


# ---------- TOFU 辅助函数 ----------

class TestTofuHelpers:
    """TOFU 首次连接相关的 sealedAuthData / unsealTofuChallenge 测试。"""

    def test_unseal_tofu_challenge_matches_python(self, crypto_page):
        """Python 端 SealedBox 加密的 challenge → JS 端可正确解封。"""
        phone_priv = PrivateKey.generate()
        phone_pub_b64 = _to_b64(bytes(phone_priv.public_key))
        pc_priv = PrivateKey.generate()
        nonce = bytes(range(16))

        # Python 端：SealedBox(phone_public) 加密 challenge
        inner = json.dumps({
            "pk": _to_b64(bytes(pc_priv.public_key)),
            "nonce": _to_b64(nonce),
        }).encode("utf-8")
        sealed = SealedBox(phone_priv.public_key).encrypt(inner)

        result = crypto_page.evaluate("""
            ({ phonePrivB64, sealedB64 }) => {
                const phonePriv = sodium.from_base64(phonePrivB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const sealed = sodium.from_base64(sealedB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const result = unsealTofuChallenge(sealed, phonePriv);
                return {
                    pcPubB64: sodium.to_base64(result.pcPublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                    nonceB64: sodium.to_base64(result.nonce, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                };
            }
        """, {
            "phonePrivB64": _to_b64(bytes(phone_priv)),
            "sealedB64": _to_b64(sealed),
        })

        assert result["pcPubB64"] == _to_b64(bytes(pc_priv.public_key))
        assert result["nonceB64"] == _to_b64(nonce)

    def test_unseal_assigned_pin_matches_python(self, crypto_page):
        """PC 指派并密封下发的识别码 → JS 端可正确解封（含同源 nonce）。

        这条通道是「抄不走」的关键：识别码只以密文形态过网，且密文只对持有
        sk_手机 的那一方可读（design §5.5.1）。
        """
        phone_priv = PrivateKey.generate()
        nonce = bytes(range(16))
        pin = "3847"

        inner = json.dumps({
            "pin": pin,
            "nonce": _to_b64(nonce),
        }).encode("utf-8")
        sealed = SealedBox(phone_priv.public_key).encrypt(inner)

        result = crypto_page.evaluate("""
            ({ phonePrivB64, sealedB64 }) => {
                const phonePriv = sodium.from_base64(phonePrivB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const sealed = sodium.from_base64(sealedB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const result = unsealAssignedPin(sealed, phonePriv);
                return {
                    pin: result.pin,
                    nonceB64: sodium.to_base64(result.nonce, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                };
            }
        """, {
            "phonePrivB64": _to_b64(bytes(phone_priv)),
            "sealedB64": _to_b64(sealed),
        })

        assert result["pin"] == pin
        assert result["nonceB64"] == _to_b64(nonce)

    def test_phone_public_key_getter(self, crypto_page):
        """Provider 暴露 phonePublicKey 供 TOFU 明文 auth 使用。"""
        result = crypto_page.evaluate("""
            () => {
                const p = new XChaCha20Provider();
                p.initKeypair();
                return {
                    hasGetter: typeof p.phonePublicKey !== 'undefined',
                    length: p.phonePublicKey.length,
                };
            }
        """)
        assert result["hasGetter"] is True
        assert result["length"] == 32


# ---------- NaClBoxProvider ----------

class TestNaClBoxProvider:
    def test_algorithm_name(self, crypto_page):
        assert crypto_page.evaluate("() => NaClBoxProvider.algorithmName") == "xsalsa20"

    def test_keypair_generation(self, crypto_page):
        result = crypto_page.evaluate("""
            () => {
                const p = new NaClBoxProvider();
                p.initKeypair();
                return { privLen: p._phonePrivate.length, pubLen: p._phonePublicKey.length };
            }
        """)
        assert result["privLen"] == 32
        assert result["pubLen"] == 32

    def test_js_roundtrip_phone_encrypt_pc_decrypt(self, crypto_page):
        """JS 手机端加密 → JS PC 端（sodium API）解密。"""
        result = crypto_page.evaluate("""
            () => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                const pcKp = sodium.crypto_box_keypair();
                phone.setPcPublicKey(pcKp.publicKey);

                const pt = sodium.from_string('{"type":"send","text":"hello"}');
                const encrypted = phone.encrypt(pt);

                // PC 端：ECDH + BLAKE2b KDF 派生同一对称密钥，afternm 解密，剥 8B seq 前缀
                const shared = sodium.crypto_scalarmult(pcKp.privateKey, phone._phonePublicKey);
                const key = sodium.crypto_generichash(32, shared);
                const nonce = encrypted.slice(0, sodium.crypto_box_NONCEBYTES);
                const ct = encrypted.slice(sodium.crypto_box_NONCEBYTES);
                const body = sodium.crypto_box_open_easy_afternm(ct, nonce, key);
                return sodium.to_string(body.slice(8));
            }
        """)
        assert json.loads(result)["text"] == "hello"

    def test_js_roundtrip_pc_encrypt_phone_decrypt(self, crypto_page):
        """JS PC 端（sodium API）加密 → JS 手机端 Provider 解密。"""
        result = crypto_page.evaluate("""
            () => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                const pcKp = sodium.crypto_box_keypair();
                phone.setPcPublicKey(pcKp.publicKey);

                const pt = sodium.from_string('{"type":"preview","text":"world"}');
                // PC 端：派生同一对称密钥，明文前置 8B seq(=0) 后 afternm 加密
                const shared = sodium.crypto_scalarmult(pcKp.privateKey, phone._phonePublicKey);
                const key = sodium.crypto_generichash(32, shared);
                const nonce = sodium.randombytes_buf(sodium.crypto_box_NONCEBYTES);
                const body = new Uint8Array(8 + pt.length);
                body.set(new Uint8Array(8), 0);
                body.set(pt, 8);
                const ct = sodium.crypto_box_easy_afternm(body, nonce, key);
                const combined = new Uint8Array(nonce.length + ct.length);
                combined.set(nonce, 0);
                combined.set(ct, nonce.length);

                const decrypted = phone.decrypt(combined);
                return sodium.to_string(decrypted);
            }
        """)
        assert json.loads(result)["text"] == "world"

    def test_set_pc_public_key_invalidates_cached_session_key(self, crypto_page):
        """换掉 PC 公钥必须让缓存的会话密钥作废（理由见 XChaCha20Provider 同名用例）。"""
        result = crypto_page.evaluate("""
            () => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                const pcA = sodium.crypto_box_keypair();
                const pcB = sodium.crypto_box_keypair();
                phone.setPcPublicKey(pcA.publicKey);
                phone.encrypt(sodium.from_string('with-a'));    /* 派生并缓存 */
                phone.setPcPublicKey(pcB.publicKey);            /* 换成 B */
                const ct = phone.encrypt(sodium.from_string('with-b'));

                const nonceSize = sodium.crypto_box_NONCEBYTES;
                const nonce = ct.slice(0, nonceSize);
                const body = ct.slice(nonceSize);
                const keyFor = (pcPriv) => sodium.crypto_generichash(
                    32, sodium.crypto_scalarmult(pcPriv, phone._phonePublicKey));
                let textB = null, threwWithA = false;
                try {
                    textB = sodium.to_string(sodium.crypto_box_open_easy_afternm(
                        body, nonce, keyFor(pcB.privateKey)).slice(8));
                } catch (e) { textB = 'FAILED: ' + e.message; }
                try {
                    sodium.crypto_box_open_easy_afternm(body, nonce, keyFor(pcA.privateKey));
                } catch (e) { threwWithA = true; }
                return { textB: textB, threwWithA: threwWithA };
            }
        """)
        assert result["textB"] == "with-b", "换公钥后必须用新公钥派生的密钥加密"
        assert result["threwWithA"] is True, "旧会话密钥不应继续生效"

    def test_cross_platform_js_encrypt_py_decrypt(self, crypto_page):
        """跨平台：JS 手机端加密 → Python PC 端解密。"""
        pc_priv = PrivateKey.generate()
        pc_pub_b64 = _to_b64(bytes(pc_priv.public_key))

        js_result = crypto_page.evaluate("""
            (pcPubB64) => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.from_base64(pcPubB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                const pt = sodium.from_string('{"type":"send","text":"cross-js2py"}');
                const encrypted = phone.encrypt(pt);
                return {
                    encryptedB64: sodium.to_base64(encrypted, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                    phonePubB64: sodium.to_base64(phone._phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                };
            }
        """, pc_pub_b64)

        phone_pub = PublicKey(_from_b64(js_result["phonePubB64"]))
        # 与 JS 端同一派生链：ECDH → BLAKE2b(32) → SecretBox（afternm 语义）
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        box = SecretBox(blake2b(shared, digest_size=32).digest())
        # 解密结果为 seq(8B 大端) || 明文，剥前缀（首帧 seq=0）
        plaintext = box.decrypt(_from_b64(js_result["encryptedB64"]))[8:]
        assert json.loads(plaintext)["text"] == "cross-js2py"

    def test_cross_platform_py_encrypt_js_decrypt(self, crypto_page):
        """跨平台：Python PC 端加密 → JS 手机端解密。"""
        pc_priv = PrivateKey.generate()
        pc_pub_b64 = _to_b64(bytes(pc_priv.public_key))

        # Step 1: JS 创建手机 Provider，保存到 window，返回公钥
        js_setup = crypto_page.evaluate("""
            (pcPubB64) => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.from_base64(pcPubB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                window.__testPhone = phone;
                return sodium.to_base64(phone._phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """, pc_pub_b64)

        # Step 2: Python 加密（与 JS 端同一派生链，明文前置 8B seq(=0)）
        phone_pub = PublicKey(_from_b64(js_setup))
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        box = SecretBox(blake2b(shared, digest_size=32).digest())
        plaintext = (0).to_bytes(8, "big") + b'{"type":"preview","text":"cross-py2js"}'
        nonce = random_bytes(SecretBox.NONCE_SIZE)
        encrypted = bytes(box.encrypt(plaintext, nonce))
        encrypted_b64 = _to_b64(encrypted)

        # Step 3: JS 解密
        result = crypto_page.evaluate("""
            (encryptedB64) => {
                const raw = sodium.from_base64(encryptedB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const decrypted = window.__testPhone.decrypt(raw);
                return sodium.to_string(decrypted);
            }
        """, encrypted_b64)
        assert json.loads(result)["text"] == "cross-py2js"

    def test_cross_platform_full_handshake(self, crypto_page):
        """跨平台三步握手 + 双向加密通信（Python 当服务端，JS 当手机）。

        握手帧的组装/识别归 SecureClient，Provider 只碰原始字节——所以这里
        JS 侧直接用 MessagePack 解析解密后的明文，等价于 SecureClient 的职责。
        """
        pc_priv = PrivateKey.generate()
        pc_pub_b64 = _to_b64(bytes(pc_priv.public_key))

        # JS 端：创建手机 Provider，生成 auth data
        js_auth = crypto_page.evaluate("""
            (pcPubB64) => {
                const phone = new NaClBoxProvider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.from_base64(pcPubB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                window.__testPhone = phone;
                const authData = phone.makeAuthData();
                return sodium.to_base64(authData, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """, pc_pub_b64)

        # Python 端：解封 auth data——新语义密封的是 {"algo","pk"} JSON
        sealed = _from_b64(js_auth)
        sb = SealedBox(pc_priv)
        inner = json.loads(sb.decrypt(sealed).decode("utf-8"))
        assert inner["algo"] == "xsalsa20"
        phone_pub = PublicKey(_from_b64(inner["pk"]))
        # 与 JS 端同一派生链：ECDH → BLAKE2b(32) → SecretBox（afternm 语义）
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        box = SecretBox(blake2b(shared, digest_size=32).digest())

        # 第二步：Python 下发 auth_challenge（下行 0 号帧 = 8B seq 前缀 + msgpack）
        nonce = random_bytes(16)
        ch_plain = (0).to_bytes(8, "big") + frame_encode(
            {"type": "auth_challenge", "nonce": nonce}
        )
        ch_encrypted = bytes(box.encrypt(ch_plain, random_bytes(SecretBox.NONCE_SIZE)))

        # 第三步：JS 解密挑战并回 auth_proof（上行 0 号帧）
        proof_b64 = crypto_page.evaluate("""
            (chB64) => {
                const raw = sodium.from_base64(chB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const msg = MessagePack.decode(window.__testPhone.decrypt(raw));
                if (msg.type !== 'auth_challenge') return null;
                const proof = MessagePack.encode({ type: 'auth_proof', nonce: msg.nonce });
                return sodium.to_base64(window.__testPhone.encrypt(proof),
                                        sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """, _to_b64(ch_encrypted))
        assert proof_b64, "JS 未回 auth_proof"

        # Python 端解出 proof 并校验 nonce（等价于 SecureSession.verify_auth_proof）
        proof_pt = bytes(box.decrypt(_from_b64(proof_b64)))
        assert int.from_bytes(proof_pt[:8], "big") == 0  # proof 是上行 0 号帧
        proof = frame_decode(proof_pt[8:])
        assert proof["type"] == "auth_proof"
        assert proof["nonce"] == nonce

        # 握手完成后 JS 继续发应用层消息，Python 能解开（seq 递增到 1）
        js_msg = crypto_page.evaluate("""
            () => {
                const pt = MessagePack.encode({ type: 'send', text: 'handshake-ok' });
                return sodium.to_base64(window.__testPhone.encrypt(pt),
                                        sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """)
        send_pt = bytes(box.decrypt(_from_b64(js_msg)))
        assert int.from_bytes(send_pt[:8], "big") == 1
        assert frame_decode(send_pt[8:])["text"] == "handshake-ok"


# ---------- XChaCha20Provider ----------

class TestXChaCha20Provider:
    def test_algorithm_name(self, crypto_page):
        assert crypto_page.evaluate("() => XChaCha20Provider.algorithmName") == "xchacha20"

    def test_js_roundtrip(self, crypto_page):
        """JS 端自加密自解密往返。"""
        result = crypto_page.evaluate("""
            () => {
                const phone = new XChaCha20Provider();
                phone.initKeypair();
                const pcKp = sodium.crypto_box_keypair();
                phone.setPcPublicKey(pcKp.publicKey);

                const pt = sodium.from_string('{"type":"send","text":"xchacha-rt"}');
                const encrypted = phone.encrypt(pt);

                // PC 端：ECDH + BLAKE2b KDF + XChaCha20 解密。
                // 与 XSalsa20 完全同构：seq 是**明文前 8 字节**（不是 aad），
                // 解密后再剥（http-upload-design.md §5.8 统一了两条实现路径）。
                const shared = sodium.crypto_scalarmult(pcKp.privateKey, phone._phonePublicKey);
                const key = sodium.crypto_generichash(32, shared);
                const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
                const nonce = encrypted.slice(0, nonceSize);
                const ct = encrypted.slice(nonceSize);
                const body = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    null, ct, null, nonce, key);
                return sodium.to_string(body.slice(8));
            }
        """)
        assert json.loads(result)["text"] == "xchacha-rt"

    def test_ciphertext_overhead_is_48_for_both_algorithms(self, crypto_page):
        """两算法的密文长度都是 **明文 + 48**（nonce 24 + seq 前缀 8 + tag 16）。

        这是服务端 ``CHUNK_OVERHEAD`` 与 `Content-Length = X-Pm-Len + 48` 这条
        预计算规则的 JS 侧依据；两算法不一致就会有一半的片被 400 挡掉。
        """
        result = crypto_page.evaluate("""
            () => {
                const out = {};
                for (const [name, Cls] of Object.entries(
                        {xsalsa20: NaClBoxProvider, xchacha20: XChaCha20Provider})) {
                    const p = new Cls();
                    p.initKeypair();
                    p.setPcPublicKey(sodium.crypto_box_keypair().publicKey);
                    const empty = p.encrypt(new Uint8Array(0));
                    const full = p.encrypt(new Uint8Array(7));
                    out[name] = [empty.length, full.length - 7];
                }
                return out;
            }
        """)
        assert result["xsalsa20"] == [48, 48]
        assert result["xchacha20"] == [48, 48]

    def test_decrypt_rejects_replayed_seq(self, crypto_page):
        """前缀路径下「重放」的判据不变：解密成功、seq 不递增即拒。"""
        result = crypto_page.evaluate("""
            () => {
                const phone = new XChaCha20Provider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.crypto_box_keypair().publicKey);
                const ct = phone.encrypt(sodium.from_string('once'));
                const first = sodium.to_string(phone.decrypt(ct));
                let replayThrew = false;
                try { phone.decrypt(ct); } catch (e) { replayThrew = true; }
                return { first: first, replayThrew: replayThrew };
            }
        """)
        assert result["first"] == "once"
        assert result["replayThrew"] is True

    def test_set_pc_public_key_invalidates_cached_session_key(self, crypto_page):
        """换掉 PC 公钥（服务重启后重配对）必须让缓存的会话密钥作废。

        ``_sharedKey`` 是懒派生的：命中缓存就直接返回，而它的输入含 PC 公钥。
        手机在同一个页面内往往已经用**上一任** PC 公钥派生过会话密钥，此时只
        清 localStorage 不重置 Provider，手机就会拿旧密钥加密 auth_proof——
        服务端解不开，日志上表现为 "Auth proof rejected"，且刷新页面才恢复。
        """
        result = crypto_page.evaluate("""
            () => {
                const phone = new XChaCha20Provider();
                phone.initKeypair();
                const pcA = sodium.crypto_box_keypair();
                const pcB = sodium.crypto_box_keypair();
                phone.setPcPublicKey(pcA.publicKey);
                phone.encrypt(sodium.from_string('with-a'));    /* 派生并缓存 */
                phone.setPcPublicKey(pcB.publicKey);            /* 换成 B */
                const ct = phone.encrypt(sodium.from_string('with-b'));

                const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
                const nonce = ct.slice(0, nonceSize);
                const body = ct.slice(nonceSize);
                const keyFor = (pcPriv) => sodium.crypto_generichash(
                    32, sodium.crypto_scalarmult(pcPriv, phone._phonePublicKey));
                let textB = null, threwWithA = false;
                try {
                    textB = sodium.to_string(sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                        null, body, null, nonce, keyFor(pcB.privateKey)).slice(8));
                } catch (e) { textB = 'FAILED: ' + e.message; }
                try {
                    sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                        null, body, null, nonce, keyFor(pcA.privateKey));
                } catch (e) { threwWithA = true; }
                return { textB: textB, threwWithA: threwWithA };
            }
        """)
        assert result["textB"] == "with-b", "换公钥后必须用新公钥派生的密钥加密"
        assert result["threwWithA"] is True, "旧会话密钥不应继续生效"

    def test_cross_platform_js_encrypt_py_decrypt(self, crypto_page):
        """跨平台：JS XChaCha20 加密 → Python 解密。"""
        pc_priv = PrivateKey.generate()
        pc_pub_b64 = _to_b64(bytes(pc_priv.public_key))

        js_result = crypto_page.evaluate("""
            (pcPubB64) => {
                const phone = new XChaCha20Provider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.from_base64(pcPubB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                const pt = sodium.from_string('{"type":"send","text":"xchacha-js2py"}');
                const encrypted = phone.encrypt(pt);
                return {
                    encryptedB64: sodium.to_base64(encrypted, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                    phonePubB64: sodium.to_base64(phone._phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                };
            }
        """, pc_pub_b64)

        phone_pub = PublicKey(_from_b64(js_result["phonePubB64"]))
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        aead = Aead(blake2b(shared, digest_size=32).digest())
        # JS 端与 XSalsa20 同构：seq 是明文前 8 字节（首帧 seq=0），解密后剥掉
        plaintext = aead.decrypt(_from_b64(js_result["encryptedB64"]))
        assert int.from_bytes(plaintext[:8], "big") == 0
        assert json.loads(plaintext[8:])["text"] == "xchacha-js2py"

    def test_cross_platform_py_encrypt_js_decrypt(self, crypto_page):
        """跨平台：Python XChaCha20 加密 → JS 解密。"""
        pc_priv = PrivateKey.generate()
        pc_pub_b64 = _to_b64(bytes(pc_priv.public_key))

        # Step 1: JS 创建手机 Provider，保存到 window
        js_setup = crypto_page.evaluate("""
            (pcPubB64) => {
                const phone = new XChaCha20Provider();
                phone.initKeypair();
                phone.setPcPublicKey(sodium.from_base64(pcPubB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                window.__testXChaChaPhone = phone;
                return sodium.to_base64(phone._phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """, pc_pub_b64)

        # Step 2: Python ECDH + BLAKE2b KDF + XChaCha20 加密（seq 打进明文前缀）
        phone_pub = PublicKey(_from_b64(js_setup))
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        aead = Aead(blake2b(shared, digest_size=32).digest())
        plaintext = (0).to_bytes(8, "big") + b'{"type":"preview","text":"xchacha-py2js"}'
        encrypted = bytes(aead.encrypt(plaintext))
        encrypted_b64 = _to_b64(encrypted)

        # Step 3: JS 解密
        result = crypto_page.evaluate("""
            (encryptedB64) => {
                const raw = sodium.from_base64(encryptedB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const decrypted = window.__testXChaChaPhone.decrypt(raw);
                return sodium.to_string(decrypted);
            }
        """, encrypted_b64)
        assert json.loads(result)["text"] == "xchacha-py2js"


# ---------- 跨算法隔离 ----------

class TestCrossAlgorithm:
    def test_xsalsa20_cannot_decrypt_xchacha20(self, crypto_page):
        """XSalsa20 Provider 无法解密 XChaCha20 的密文。"""
        result = crypto_page.evaluate("""
            () => {
                const xchacha = new XChaCha20Provider();
                xchacha.initKeypair();
                const pcKp = sodium.crypto_box_keypair();
                xchacha.setPcPublicKey(pcKp.publicKey);
                const encrypted = xchacha.encrypt(sodium.from_string("secret"));

                const nacl = new NaClBoxProvider();
                nacl.initKeypair();
                nacl.setPcPublicKey(pcKp.publicKey);
                try {
                    nacl.decrypt(encrypted);
                    return { threw: false };
                } catch (e) {
                    return { threw: true };
                }
            }
        """)
        assert result["threw"] is True


# ---------- 分片上传：MAC 原语与独立 Provider 实例 ----------

class TestUploadMacAndSessionKey:
    """docs/http-upload-design.md §5.3 / §5.8 / §5.9。

    这一层是**两端必须逐字节一致**的部分：MAC 算法、被签内容、以及「上传用独立
    provider 实例」这条约束。任一处两端不一致，都只在真机上传时才暴露（而且表现
    为"这一片服务端说签名错/解不开"），只跑 Python 侧的测试抓不到。
    """

    def test_upload_mac_matches_python(self, crypto_page):
        """JS uploadMac ≡ Python upload_mac（keyed BLAKE2b，msg = PUT\\nsid\\noffset\\nlen）。"""
        k_mac = bytes(range(32))
        sid = "AbC-123_xyz"
        result = crypto_page.evaluate("""
            ({ kMacB64, sid, offset, len }) => {
                const kMac = sodium.from_base64(kMacB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const mac = uploadMac(kMac, sid, offset, len);
                return { macB64: encodeMac(mac), rawLen: mac.length };
            }
        """, {"kMacB64": _to_b64(k_mac), "sid": sid, "offset": 15, "len": 1024})

        assert result["rawLen"] == 32
        expected = upload_mac(k_mac, sid, 15, 1024)
        assert result["macB64"] == encode_mac(expected)
        assert _from_b64(result["macB64"]) == expected

    def test_upload_mac_is_bound_to_all_three_fields(self, crypto_page):
        """sid / offset / len 三项缺一不可——只签 len 的话，一份合法签名可以配上
        改过的偏移，去覆盖文件的别的位置（§5.3）。"""
        k_mac = bytes(range(32))
        result = crypto_page.evaluate("""
            ({ kMacB64, sid }) => {
                const kMac = sodium.from_base64(kMacB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                return {
                    base: encodeMac(uploadMac(kMac, sid, 0, 100)),
                    otherOffset: encodeMac(uploadMac(kMac, sid, 1, 100)),
                    otherLen: encodeMac(uploadMac(kMac, sid, 0, 101)),
                    otherSid: encodeMac(uploadMac(kMac, 'other', 0, 100)),
                };
            }
        """, {"kMacB64": _to_b64(k_mac), "sid": "s1"})
        assert len({result["base"], result["otherOffset"],
                    result["otherLen"], result["otherSid"]}) == 4

    @pytest.mark.parametrize("algo", ["xchacha20", "xsalsa20"])
    def test_injected_session_key_interops_with_python(self, crypto_page, algo):
        """injectSessionKey 建的实例与 Python ``create_provider(algo, k_body)`` 互通。

        上传的 k_body 是服务端现生成、随 ``upload_ready`` 下发的随机对称密钥，
        **不是** ECDH 协商来的 ⇒ JS 侧必须能跳过密钥交换直接注入（§5.9）。

        ⚠️ 两种算法都要测：xchacha20 是协商优先级最高的那个、上传几乎总走它，
        曾经就是它漏了 injectSessionKey（只有 xsalsa20 有）⇒ 真机上
        ``upload_ready`` 之后的第一次加密直接 TypeError。
        """
        k_body = bytes(range(32))
        result = crypto_page.evaluate("""
            ({ kBodyB64, algo }) => {
                const Cls = algo === 'xchacha20' ? XChaCha20Provider : NaClBoxProvider;
                const p = new Cls();
                p.injectSessionKey(sodium.from_base64(kBodyB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                const ct = p.encrypt(sodium.from_string('chunk-payload'));
                return { b64: sodium.to_base64(ct, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                         keyLen: p._sharedKey.length };
            }
        """, {"kBodyB64": _to_b64(k_body), "algo": algo})

        assert result["keyLen"] == 32
        provider = create_provider(algo, k_body)
        assert provider.decrypt(_from_b64(result["b64"])) == b"chunk-payload"

    def test_both_providers_expose_the_full_interface(self, crypto_page):
        """两个 Provider 的对外接口必须**逐项对齐**（含上传要用的那几个）。

        少一个方法不会在加载时报错，只在那条路第一次被走到时 TypeError ——
        所以这里把接口面钉下来。
        """
        result = crypto_page.evaluate("""
            () => {
                const need = ['initKeypair', 'setPcPublicKey', 'injectSessionKey',
                              'makeAuthData', 'encrypt', 'decrypt', 'reset'];
                const out = {};
                for (const [name, Cls] of Object.entries(
                        {xsalsa20: NaClBoxProvider, xchacha20: XChaCha20Provider})) {
                    const p = new Cls();
                    out[name] = need.filter(m => typeof p[m] !== 'function');
                }
                return out;
            }
        """)
        assert result["xsalsa20"] == []
        assert result["xchacha20"] == []

    def test_injected_key_skips_ecdh_entirely(self, crypto_page):
        """注入之后**不再**走 ECDH：没调用过 setPcPublicKey 也能加密。

        （上传那条路从没有人调用 setPcPublicKey —— 手机侧根本没有 PC 私钥可用。）
        """
        result = crypto_page.evaluate("""
            ({ kBodyB64 }) => {
                const p = new XChaCha20Provider();
                p.injectSessionKey(sodium.from_base64(kBodyB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING));
                return { len: p.encrypt(sodium.from_string('no-ecdh-needed')).length,
                         hasPcKey: p._pcPublicKey !== null };
            }
        """, {"kBodyB64": _to_b64(bytes(range(32)))})
        assert result["len"] == len("no-ecdh-needed") + 48
        assert result["hasPcKey"] is False

    def test_upload_provider_instance_is_independent(self, crypto_page):
        """⚠️ 上传必须用**另一个实例**：同一实例的 seq 是单个计数器，两条独立 TCP
        （WS + HTTP）的到达顺序不由发送方决定，共用必然出现「必有一条解不开」（§5.8）。

        这里把这条约束变成可执行断言：两个实例各自从 seq=0 开始、互不干扰。
        """
        k_body = bytes(range(32))
        result = crypto_page.evaluate("""
            ({ kBodyB64 }) => {
                const raw = sodium.from_base64(kBodyB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const wsSide = new XChaCha20Provider();
                wsSide.injectSessionKey(raw);
                const upSide = new XChaCha20Provider();
                upSide.injectSessionKey(raw);
                wsSide.encrypt(sodium.from_string('ws-frame-1'));
                wsSide.encrypt(sodium.from_string('ws-frame-2'));
                // 上传侧的第 1 片仍是 seq=0，没被 WS 侧推着走
                return sodium.to_base64(upSide.encrypt(sodium.from_string('upload-chunk-1')),
                                        sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            }
        """, {"kBodyB64": _to_b64(k_body)})
        # Python 侧同一把密钥、也从 0 开始 ⇒ 解得开就证明上传侧确实是 seq=0
        assert create_provider("xchacha20", k_body).decrypt(_from_b64(result)) == b"upload-chunk-1"
