/**
 * CryptoProvider 加密算法提供者接口及具体实现。
 *
 * 依赖全局 sodium 对象（libsodium.js），需在 sodium.ready 后使用。
 *
 * 接口约定（与 Python 端 phonemic/tunnel/crypto/ 一一对应）：
 * - encrypt(plaintextBytes) → nonce+ciphertext 拼接的 Uint8Array
 * - decrypt(ciphertextBytes) → plaintext Uint8Array
 * - 防重放 seq 由 Provider 内部承载（AAD 优先 / 8 字节前缀兜底），
 *   不进应用层 JSON，调用方不可见
 * - makeAuthData(): 用 PC 公钥 SealedBox 密封 {"algo","pk"} JSON——
 *   algo 在密文内部，不再明文传输
 * - 所有 base64 编解码由 SecureClient 处理，Provider 只操作原始字节
 */

// 8 字节大端 seq 编解码（与 Python 端 _SEQ_LEN=8 / to_bytes(8,'big') 一致）
function seqToBytes(n) {
    const b = new Uint8Array(8);
    for (let i = 7; i >= 0; i--) { b[i] = n & 0xff; n = Math.floor(n / 256); }
    return b;
}
function seqFromBytes(b) {
    let n = 0;
    for (let i = 0; i < 8; i++) n = n * 256 + b[i];
    return n;
}

// 密封 auth 数据：{"algo": <算法名>, "pk": <手机公钥 base64url>}，
// algo 只出现在密文内部（与 Python 端 KeyExchange.handle_auth 约定一致）
function sealedAuthData(providerName, phonePublicKey, pcPublicKey) {
    const inner = JSON.stringify({
        algo: providerName,
        pk: sodium.to_base64(phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
    });
    return sodium.crypto_box_seal(sodium.from_string(inner), pcPublicKey);
}

class NaClBoxProvider {
    static get algorithmName() { return 'xsalsa20'; }
    constructor() {
        this._phonePrivate = null;
        this._phonePublicKey = null;
        this._pcPublicKey = null;   // Uint8Array
        this._sharedKey = null;
        // 防重放 seq：Provider 内部状态，外部不可见
        this._txSeq = 0;
        this._rxSeq = 0;
    }
    initKeypair() {
        const kp = sodium.crypto_box_keypair();
        this._phonePrivate = kp.privateKey;
        this._phonePublicKey = kp.publicKey;
    }
    setPcPublicKey(rawBytes) { this._pcPublicKey = rawBytes; }
    makeAuthData() {
        if (!this._pcPublicKey) return null;
        return sealedAuthData('xsalsa20', this._phonePublicKey, this._pcPublicKey);
    }
    _deriveSharedKey() {
        if (this._sharedKey) return this._sharedKey;
        const shared = sodium.crypto_scalarmult(this._phonePrivate, this._pcPublicKey);
        // 与 Python 端一致：BLAKE2b(32) 派生会话密钥，之后按对称密钥使用
        // （等价于 crypto_box_afternm，与 Python 端 SecretBox 同一原语）
        this._sharedKey = sodium.crypto_generichash(32, shared);
        return this._sharedKey;
    }
    encrypt(plaintextBytes) {
        const key = this._deriveSharedKey();
        // Box 无 aad：seq 以 8 字节大端前缀焊入明文后整体加密
        const body = new Uint8Array(8 + plaintextBytes.length);
        body.set(seqToBytes(this._txSeq++), 0);
        body.set(plaintextBytes, 8);
        const nonce = sodium.randombytes_buf(sodium.crypto_box_NONCEBYTES);
        const ct = sodium.crypto_box_easy_afternm(body, nonce, key);
        const combined = new Uint8Array(nonce.length + ct.length);
        combined.set(nonce, 0);
        combined.set(ct, nonce.length);
        return combined;
    }
    decrypt(ciphertextBytes) {
        const key = this._deriveSharedKey();
        const nonce = ciphertextBytes.slice(0, sodium.crypto_box_NONCEBYTES);
        const ct = ciphertextBytes.slice(sodium.crypto_box_NONCEBYTES);
        let body;
        try {
            body = sodium.crypto_box_open_easy_afternm(ct, nonce, key);
        } catch (e) {
            throw new Error('[SEC] decrypt failed (MAC mismatch)');
        }
        // 解密成功即可读 seq：在解析应用层数据之前完成校验
        const seq = seqFromBytes(body.slice(0, 8));
        if (seq !== this._rxSeq) {
            throw new Error('[SEC] replay detected (seq ' + seq + ' != expected ' + this._rxSeq + ')');
        }
        this._rxSeq++;
        return body.slice(8);
    }
    handleAuthAck(rawBytes) {
        try {
            // 首个下行帧：走统一 decrypt 路径（seq=0 校验并推进 _rxSeq）
            const pt = this.decrypt(rawBytes);
            const msg = JSON.parse(sodium.to_string(pt));
            return msg.status === 'OK';
        } catch (e) {
            console.error('[SEC] auth_ack decrypt failed:', e);
            return false;
        }
    }
    reset() {
        this._txSeq = 0;
        this._rxSeq = 0;
    }
}

class XChaCha20Provider {
    static get algorithmName() { return 'xchacha20'; }
    constructor() {
        this._phonePrivate = null;
        this._phonePublicKey = null;
        this._pcPublicKey = null;
        this._sharedKey = null;
        this._txSeq = 0;
        this._rxSeq = 0;
    }
    initKeypair() {
        const kp = sodium.crypto_box_keypair();
        this._phonePrivate = kp.privateKey;
        this._phonePublicKey = kp.publicKey;
    }
    setPcPublicKey(rawBytes) { this._pcPublicKey = rawBytes; }
    makeAuthData() {
        if (!this._pcPublicKey) return null;
        return sealedAuthData('xchacha20', this._phonePublicKey, this._pcPublicKey);
    }
    _deriveSharedKey() {
        if (this._sharedKey) return this._sharedKey;
        const shared = sodium.crypto_scalarmult(this._phonePrivate, this._pcPublicKey);
        // 标准 KDF：BLAKE2b 派生会话密钥（与 Python 端 blake2b 一致）
        this._sharedKey = sodium.crypto_generichash(32, shared);
        return this._sharedKey;
    }
    encrypt(plaintextBytes) {
        const key = this._deriveSharedKey();
        const nonce = sodium.randombytes_buf(sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES);
        // AEAD 支持 aad：seq 作为关联数据，解密时整体校验、明文暴露前即拒绝重放
        const ct = sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintextBytes, seqToBytes(this._txSeq++), null, nonce, key);
        const combined = new Uint8Array(nonce.length + ct.length);
        combined.set(nonce, 0);
        combined.set(ct, nonce.length);
        return combined;
    }
    decrypt(ciphertextBytes) {
        const key = this._deriveSharedKey();
        const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
        const nonce = ciphertextBytes.slice(0, nonceSize);
        const ct = ciphertextBytes.slice(nonceSize);
        let pt;
        try {
            // 签名：decrypt(secret_nonce, ciphertext, additional_data, public_nonce, key)
            pt = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                null, ct, seqToBytes(this._rxSeq), nonce, key);
        } catch (e) {
            // seq 不递增与密文被篡改都表现为 MAC 失败，不做区分
            throw new Error('[SEC] decrypt failed (MAC mismatch, possibly replay)');
        }
        this._rxSeq++;
        return pt;
    }
    handleAuthAck(rawBytes) {
        try {
            // 首个下行帧：走统一 decrypt 路径（seq=0 校验并推进 _rxSeq）
            const pt = this.decrypt(rawBytes);
            const msg = JSON.parse(sodium.to_string(pt));
            return msg.status === 'OK';
        } catch (e) {
            console.error('[SEC] auth_ack decrypt failed:', e);
            return false;
        }
    }
    reset() {
        this._txSeq = 0;
        this._rxSeq = 0;
    }
}

class PlainProvider {
    static get algorithmName() { return 'none'; }
    constructor() { this._token = null; }
    initKeypair() {}
    setPcPublicKey(rawBytes) {}
    setToken(token) { this._token = token; }
    makeAuthData() { return this._token; }
    handleAuthAck(rawBytes) { return true; }
    encrypt(plaintextBytes) { return plaintextBytes; }
    decrypt(ciphertextBytes) { return ciphertextBytes; }
    reset() {}
}

const PROVIDER_CLASSES = {
    'none': PlainProvider,
    'xsalsa20': NaClBoxProvider,
    'xchacha20': XChaCha20Provider,
};
