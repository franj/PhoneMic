"""
JS CryptoProvider 单元测试

使用 Playwright 在真实浏览器中测试 crypto_providers.js 的加密提供者。
参考 test_mobile.py 的模式：page.set_content + page.evaluate。

测试覆盖：
- PlainProvider: 明文往返
- NaClBoxProvider: XSalsa20-Poly1305 加解密往返、auth 握手
- XChaCha20Provider: XChaCha20-Poly1305 AEAD 加解密往返、auth 握手
- 跨平台互操作: JS ↔ Python（PyNaCl 模拟 PC 端）
- 跨算法隔离: 不同算法之间无法互通
"""

import base64
import json
from pathlib import Path

import pytest
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.secret import Aead
from nacl.bindings import crypto_scalarmult
from nacl.utils import random as random_bytes

pytest.importorskip("playwright")

RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"
SODIUM_JS = (RES_DIR / "sodium.js").read_text(encoding="utf-8")
CRYPTO_JS = (RES_DIR / "crypto_providers.js").read_text(encoding="utf-8")

TEST_HTML = (
    "<!DOCTYPE html><html><head>"
    f"<script>{SODIUM_JS}</script>"
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


# ---------- PlainProvider ----------

class TestPlainProvider:
    def test_algorithm_name(self, crypto_page):
        result = crypto_page.evaluate("() => PlainProvider.algorithmName")
        assert result == "none"

    def test_encrypt_decrypt_roundtrip(self, crypto_page):
        result = crypto_page.evaluate("""
            () => {
                const p = new PlainProvider();
                const data = new Uint8Array([1, 2, 3, 4, 5]);
                const encrypted = p.encrypt(data);
                const decrypted = p.decrypt(encrypted);
                return Array.from(decrypted);
            }
        """)
        assert result == [1, 2, 3, 4, 5]

    def test_make_auth_data_returns_none(self, crypto_page):
        result = crypto_page.evaluate("() => new PlainProvider().makeAuthData()")
        assert result is None

    def test_handle_auth_ack_always_true(self, crypto_page):
        result = crypto_page.evaluate("() => new PlainProvider().handleAuthAck(new Uint8Array(0))")
        assert result is True


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

                const nonce = encrypted.slice(0, sodium.crypto_box_NONCEBYTES);
                const ct = encrypted.slice(sodium.crypto_box_NONCEBYTES);
                const decrypted = sodium.crypto_box_open_easy(ct, nonce, phone._phonePublicKey, pcKp.privateKey);
                return sodium.to_string(decrypted);
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
                const nonce = sodium.randombytes_buf(sodium.crypto_box_NONCEBYTES);
                const ct = sodium.crypto_box_easy(pt, nonce, phone._phonePublicKey, pcKp.privateKey);
                const combined = new Uint8Array(nonce.length + ct.length);
                combined.set(nonce, 0);
                combined.set(ct, nonce.length);

                const decrypted = phone.decrypt(combined);
                return sodium.to_string(decrypted);
            }
        """)
        assert json.loads(result)["text"] == "world"

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
        box = Box(pc_priv, phone_pub)
        plaintext = box.decrypt(_from_b64(js_result["encryptedB64"]))
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

        # Step 2: Python 加密
        phone_pub = PublicKey(_from_b64(js_setup))
        box = Box(pc_priv, phone_pub)
        plaintext = b'{"type":"preview","text":"cross-py2js"}'
        nonce = random_bytes(Box.NONCE_SIZE)
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
        """跨平台完整 auth 握手 + 双向加密通信。"""
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

        # Python 端：解封 auth data 获取手机公钥
        sealed = _from_b64(js_auth)
        sb = SealedBox(pc_priv)
        phone_pub = PublicKey(sb.decrypt(sealed))
        box = Box(pc_priv, phone_pub)

        # Python 端：生成加密的 auth_ack
        ack_payload = json.dumps({"status": "OK", "ts": 12345}).encode("utf-8")
        ack_nonce = random_bytes(Box.NONCE_SIZE)
        ack_encrypted = bytes(box.encrypt(ack_payload, ack_nonce))
        ack_b64 = _to_b64(ack_encrypted)

        # JS 端：处理 auth_ack，然后加密消息发回
        js_result = crypto_page.evaluate("""
            (ackB64) => {
                const raw = sodium.from_base64(ackB64, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                const ok = window.__testPhone.handleAuthAck(raw);
                if (!ok) return { authenticated: false, encryptedB64: null };
                const pt = sodium.from_string('{"type":"send","text":"handshake-ok"}');
                const encrypted = window.__testPhone.encrypt(pt);
                return {
                    authenticated: true,
                    encryptedB64: sodium.to_base64(encrypted, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
                };
            }
        """, ack_b64)

        assert js_result["authenticated"] is True

        # Python 端解密 JS 消息
        plaintext = box.decrypt(_from_b64(js_result["encryptedB64"]))
        assert json.loads(plaintext)["text"] == "handshake-ok"


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

                // PC 端：ECDH + XChaCha20 解密
                const shared = sodium.crypto_scalarmult(pcKp.privateKey, phone._phonePublicKey);
                const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
                const nonce = encrypted.slice(0, nonceSize);
                const ct = encrypted.slice(nonceSize);
                const decrypted = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(null, ct, null, nonce, shared);
                return sodium.to_string(decrypted);
            }
        """)
        assert json.loads(result)["text"] == "xchacha-rt"

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
        aead = Aead(shared)
        plaintext = aead.decrypt(_from_b64(js_result["encryptedB64"]))
        assert json.loads(plaintext)["text"] == "xchacha-js2py"

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

        # Step 2: Python ECDH + XChaCha20 加密
        phone_pub = PublicKey(_from_b64(js_setup))
        shared = crypto_scalarmult(bytes(pc_priv), bytes(phone_pub))
        aead = Aead(shared)
        plaintext = b'{"type":"preview","text":"xchacha-py2js"}'
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
