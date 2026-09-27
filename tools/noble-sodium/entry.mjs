/**
 * noble-sodium —— libsodium.js 的替代实现（1.03MB → ~55KB，且无需 WASM）。
 *
 * 对外暴露与 libsodium.js **同名同形**的全局 `sodium`（snake_case 函数名 +
 * `ready` + `base64_VARIANT_URLSAFE_NO_PADDING`），因此 mobile.html /
 * crypto_providers.js / 测试里的 JS 片段一行都不用改。
 * 替换的是「库」，不是「调用方」——加密调用点保持原样，才有现成的测试做检验器。
 *
 * 构建：node tools/noble-sodium/build.mjs
 *
 * 与项目加密协议的对应（详见 docs/crypto-design.md）：
 * - 会话密钥 KDF = `crypto_scalarmult` + `crypto_generichash(32, shared)`
 *   → X25519 共享密钥再走 **BLAKE2b-256**。注意这**不是**标准 crypto_box 的
 *   HSalsa20 派生（那是 PyNaCl `crypto_box_beforenm` 的语义），两端手写对齐。
 * - 会话加密 = `crypto_box_easy_afternm` / `crypto_box_open_easy_afternm`
 *   → 就是对称 SecretBox（crypto_secretbox），密钥由上一步 KDF 直接给出。
 * - 密封盒 = `crypto_box_seal` / `crypto_box_seal_open` → **标准 SealedBox**
 *   （临时密钥 + HSalsa20 派生 + SecretBox），与 Python 端 PyNaCl `SealedBox`
 *   互通。所以 hsalsa 只出现在这一条路径上，别和 KDF 混。
 * - AEAD = `crypto_aead_xchacha20poly1305_ietf_*`，aad 承载防重放 seq。
 */
import { x25519 } from '@noble/curves/ed25519.js';
import { blake2b } from '@noble/hashes/blake2.js';
import { xchacha20poly1305 } from '@noble/ciphers/chacha.js';
import { hsalsa, xsalsa20poly1305 } from '@noble/ciphers/salsa.js';
import {
  bytesToUtf8,
  concatBytes,
  randomBytes,
  utf8ToBytes,
} from '@noble/ciphers/utils.js';

// ---------- 常量（取值与 libsodium 一致）----------

const BOX_NONCEBYTES = 24;
const BOX_PUBLICKEYBYTES = 32;
const BOX_SECRETKEYBYTES = 32;
const BOX_SEALBYTES = 48; // 32B 临时公钥 + 16B tag
const XCHACHA_NPUBBYTES = 24; // IETF 变体
const XCHACHA_ABYTES = 16;

// XSalsa20 的 sigma 常量「expand 32-byte k」按小端字面量
const SIGMA = new Uint32Array([0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]);
const ZERO16 = new Uint32Array(4);

// ---------- 字节 / 字 转换 ----------

/** 字节 → 小端语义的 32 位字。
 *
 * noble 的 `hsalsa` 内部对输入做「BE 主机才交换」的处理，等价于**期望原生视图
 * 语义**的字，所以这里刻意用原生 `Uint32Array` 视图而不是手工拼字。
 */
function bytesToNativeWords(bytes) {
  const copy = new Uint8Array(bytes); // 保证 byteOffset 对齐且不与调用方共享
  return new Uint32Array(copy.buffer, copy.byteOffset, copy.length >> 2);
}

/** 小端语义的 32 位字 → 字节（与主机字节序无关）。
 *
 * 输出侧必须显式转换：noble 写出的字**恒定是小端语义**，直接 `new Uint8Array(out.buffer)`
 * 在大端主机上会得到反序字节。
 */
function wordsToBytesLE(words) {
  const out = new Uint8Array(words.length * 4);
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    out[4 * i] = w & 0xff;
    out[4 * i + 1] = (w >>> 8) & 0xff;
    out[4 * i + 2] = (w >>> 16) & 0xff;
    out[4 * i + 3] = (w >>> 24) & 0xff;
  }
  return out;
}

/** HSalsa20 派生：X25519 共享密钥 → 32 字节对称密钥（≡ crypto_box_beforenm）。
 *
 * 只在 SealedBox 路径使用；项目自己的会话密钥走 BLAKE2b，不经过这里。
 */
function boxBeforenm(shared) {
  const out = new Uint32Array(8);
  hsalsa(SIGMA, bytesToNativeWords(shared), ZERO16, out);
  return wordsToBytesLE(out);
}

// ---------- 字符串 / base64 ----------

function from_string(str) {
  return utf8ToBytes(str);
}

function to_string(bytes) {
  return bytesToUtf8(bytes);
}

/** base64url 无 padding 解码（libsodium 的 base64_VARIANT_URLSAFE_NO_PADDING）。 */
function from_base64(str) {
  let s = str.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** base64url 无 padding 编码。分块拼接，避免长数组撞上调用栈/参数上限。 */
function to_base64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// ---------- X25519 ----------

function crypto_scalarmult_base(privateKey) {
  return x25519.getPublicKey(privateKey);
}

function crypto_scalarmult(privateKey, publicKey) {
  return x25519.getSharedSecret(privateKey, publicKey);
}

function crypto_box_keypair() {
  const privateKey = randomBytes(BOX_SECRETKEYBYTES);
  return {
    keyType: 'x25519',
    privateKey,
    publicKey: x25519.getPublicKey(privateKey),
  };
}

// ---------- KDF / 对称加密（会话层）----------

/** BLAKE2b-256 派生，对齐 Python 端 `blake2b(shared, digest_size=32)`。 */
function crypto_generichash(hashLength = 32, message, _key = null) {
  return blake2b(message, { dkLen: hashLength });
}

function crypto_box_easy_afternm(message, nonce, key) {
  return xsalsa20poly1305(key, nonce).encrypt(message);
}

function crypto_box_open_easy_afternm(ciphertext, nonce, key) {
  return xsalsa20poly1305(key, nonce).decrypt(ciphertext);
}

// ---------- SealedBox（标准 crypto_box_seal）----------

/** nonce = BLAKE2b(临时公钥 ‖ 收件方公钥, 24)，与 libsodium 逐字节一致。 */
function sealNonce(ephemeralPublicKey, recipientPublicKey) {
  return blake2b(concatBytes(ephemeralPublicKey, recipientPublicKey), { dkLen: BOX_NONCEBYTES });
}

function crypto_box_seal(message, recipientPublicKey) {
  const ephemeralSecretKey = randomBytes(BOX_SECRETKEYBYTES);
  const ephemeralPublicKey = x25519.getPublicKey(ephemeralSecretKey);
  const nonce = sealNonce(ephemeralPublicKey, recipientPublicKey);
  const key = boxBeforenm(x25519.getSharedSecret(ephemeralSecretKey, recipientPublicKey));
  const ciphertext = xsalsa20poly1305(key, nonce).encrypt(message);
  return concatBytes(ephemeralPublicKey, ciphertext);
}

function crypto_box_seal_open(ciphertext, recipientPublicKey, recipientSecretKey) {
  const ephemeralPublicKey = ciphertext.subarray(0, BOX_PUBLICKEYBYTES);
  const body = ciphertext.subarray(BOX_PUBLICKEYBYTES);
  const nonce = sealNonce(ephemeralPublicKey, recipientPublicKey);
  const key = boxBeforenm(x25519.getSharedSecret(recipientSecretKey, ephemeralPublicKey));
  return xsalsa20poly1305(key, nonce).decrypt(body);
}

// ---------- XChaCha20-Poly1305 AEAD（IETF 变体）----------

// libsodium 的签名是 encrypt(message, aad, secretNonce, publicNonce, key)；
// secretNonce 不参与 IETF 变体运算，传 null 即可。aad 为 null 视作空。
function crypto_aead_xchacha20poly1305_ietf_encrypt(
  message, aad, _secretNonce, publicNonce, key,
) {
  return xchacha20poly1305(key, publicNonce, aad ?? new Uint8Array(0)).encrypt(message);
}

function crypto_aead_xchacha20poly1305_ietf_decrypt(
  _secretNonce, ciphertext, aad, publicNonce, key,
) {
  return xchacha20poly1305(key, publicNonce, aad ?? new Uint8Array(0)).decrypt(ciphertext);
}

// ---------- 随机 ----------

function randombytes_buf(length) {
  return randomBytes(length);
}

export {
  BOX_NONCEBYTES as crypto_box_NONCEBYTES,
  BOX_PUBLICKEYBYTES as crypto_box_PUBLICKEYBYTES,
  BOX_SECRETKEYBYTES as crypto_box_SECRETKEYBYTES,
  BOX_SEALBYTES as crypto_box_SEALBYTES,
  XCHACHA_NPUBBYTES as crypto_aead_xchacha20poly1305_ietf_NPUBBYTES,
  XCHACHA_ABYTES as crypto_aead_xchacha20poly1305_ietf_ABYTES,

  crypto_aead_xchacha20poly1305_ietf_decrypt,
  crypto_aead_xchacha20poly1305_ietf_encrypt,
  crypto_box_easy_afternm,
  crypto_box_keypair,
  crypto_box_open_easy_afternm,
  crypto_box_seal,
  crypto_box_seal_open,
  crypto_generichash,
  crypto_scalarmult,
  crypto_scalarmult_base,
  from_base64,
  from_string,
  randombytes_buf,
  to_base64,
  to_string,
};

// libsodium 的 ready 是「WASM 加载完成」的 Promise；本实现无 WASM，立即就绪。
// 必须保留：mobile.html 的 SecureClient.init() 第一个 await 就是它。
export const ready = Promise.resolve();

/** libsodium 的 base64 变体 selector；调用方只把它回传给 from/to_base64。 */
export const base64_VARIANT_URLSAFE_NO_PADDING = 1;
