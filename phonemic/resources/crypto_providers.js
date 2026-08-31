/**
 * CryptoProvider 加密算法提供者接口及具体实现。
 *
 * 依赖全局 sodium 对象（libsodium.js），需在 sodium.ready 后使用。
 *
 * 接口约定：
 * - encrypt(plaintextBytes) → nonce+ciphertext 拼接的 Uint8Array
 * - decrypt(ciphertextBytes) → plaintext Uint8Array
 * - 所有 base64 编解码由 SecureClient 处理，Provider 只操作原始字节
 */

class NaClBoxProvider {
    static get algorithmName() { return 'xsalsa20'; }
    constructor() {
        this._phonePrivate = null;
        this._phonePublicKey = null;
        this._pcPublicKey = null;   // Uint8Array
    }
    initKeypair() {
        const kp = sodium.crypto_box_keypair();
        this._phonePrivate = kp.privateKey;
        this._phonePublicKey = kp.publicKey;
    }
    setPcPublicKey(rawBytes) { this._pcPublicKey = rawBytes; }
    makeAuthData() {
        if (!this._pcPublicKey) return null;
        return sodium.crypto_box_seal(this._phonePublicKey, this._pcPublicKey);
    }
    handleAuthAck(rawBytes) {
        try {
            const nonce = rawBytes.slice(0, sodium.crypto_box_NONCEBYTES);
            const ct = rawBytes.slice(sodium.crypto_box_NONCEBYTES);
            const pt = sodium.crypto_box_open_easy(ct, nonce, this._pcPublicKey, this._phonePrivate);
            const msg = JSON.parse(sodium.to_string(pt));
            return msg.status === 'OK';
        } catch (e) {
            console.error('[SEC] auth_ack decrypt failed:', e);
            return false;
        }
    }
    encrypt(plaintextBytes) {
        const nonce = sodium.randombytes_buf(sodium.crypto_box_NONCEBYTES);
        const ct = sodium.crypto_box_easy(plaintextBytes, nonce, this._pcPublicKey, this._phonePrivate);
        const combined = new Uint8Array(nonce.length + ct.length);
        combined.set(nonce, 0);
        combined.set(ct, nonce.length);
        return combined;
    }
    decrypt(ciphertextBytes) {
        const nonce = ciphertextBytes.slice(0, sodium.crypto_box_NONCEBYTES);
        const ct = ciphertextBytes.slice(sodium.crypto_box_NONCEBYTES);
        return sodium.crypto_box_open_easy(ct, nonce, this._pcPublicKey, this._phonePrivate);
    }
}

class XChaCha20Provider {
    static get algorithmName() { return 'xchacha20'; }
    constructor() {
        this._phonePrivate = null;
        this._phonePublicKey = null;
        this._pcPublicKey = null;
        this._sharedKey = null;
    }
    initKeypair() {
        const kp = sodium.crypto_box_keypair();
        this._phonePrivate = kp.privateKey;
        this._phonePublicKey = kp.publicKey;
    }
    setPcPublicKey(rawBytes) { this._pcPublicKey = rawBytes; }
    makeAuthData() {
        if (!this._pcPublicKey) return null;
        return sodium.crypto_box_seal(this._phonePublicKey, this._pcPublicKey);
    }
    _deriveSharedKey() {
        this._sharedKey = sodium.crypto_scalarmult(this._phonePrivate, this._pcPublicKey);
    }
    handleAuthAck(rawBytes) {
        try {
            this._deriveSharedKey();
            const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
            const nonce = rawBytes.slice(0, nonceSize);
            const ct = rawBytes.slice(nonceSize);
            const pt = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(null, ct, null, nonce, this._sharedKey);
            const msg = JSON.parse(sodium.to_string(pt));
            return msg.status === 'OK';
        } catch (e) {
            console.error('[SEC] auth_ack decrypt failed:', e);
            return false;
        }
    }
    encrypt(plaintextBytes) {
        if (!this._sharedKey) this._deriveSharedKey();
        const nonce = sodium.randombytes_buf(sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES);
        const ct = sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintextBytes, null, null, nonce, this._sharedKey);
        const combined = new Uint8Array(nonce.length + ct.length);
        combined.set(nonce, 0);
        combined.set(ct, nonce.length);
        return combined;
    }
    decrypt(ciphertextBytes) {
        if (!this._sharedKey) this._deriveSharedKey();
        const nonceSize = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES;
        const nonce = ciphertextBytes.slice(0, nonceSize);
        const ct = ciphertextBytes.slice(nonceSize);
        return sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(null, ct, null, nonce, this._sharedKey);
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
}

const PROVIDER_CLASSES = {
    'none': PlainProvider,
    'xsalsa20': NaClBoxProvider,
    'xchacha20': XChaCha20Provider,
};
