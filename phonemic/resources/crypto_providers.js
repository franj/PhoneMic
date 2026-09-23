/**
 * CryptoProvider 加密算法提供者接口及具体实现。
 *
 * 依赖全局 sodium 对象（libsodium.js）与 MessagePack（msgpack.min.js），
 * 需在 sodium.ready 后使用。
 *
 * 新架构（e2ee-always-on-design.md）：
 * - 加密永远开启，不存在明文模式
 * - 认证方式：URL fragment（扫码）或 TOFU（手动审批）
 *
 * 接口约定（与 Python 端 phonemic/tunnel/crypto/ 一一对应）：
 * - encrypt(plaintextBytes) → nonce+ciphertext 拼接的 Uint8Array
 * - decrypt(ciphertextBytes) → plaintext Uint8Array
 * - 防重放 seq 由 Provider 内部承载（**两种算法统一为「明文前 8 字节」**），
 *   不进应用层报文，调用方不可见
 * - makeAuthData(pcPublicKey): 用 PC 公钥 SealedBox 密封 {"algo","pk"} JSON——
 *   algo 在密文内部，不再明文传输（URL fragment 认证 / TOFU 重连路径）
 * - injectSessionKey(rawKey): 直接注入一把对称密钥，跳过内部 ECDH——
 *   分片上传要用 k_body 建一个**独立于 WS 那条流的 Provider 实例**
 *   （docs/http-upload-design.md §5.8），而上传密钥是服务端现生成、不是协商来的
 * - Provider 只操作原始字节；帧编解码与握手帧（auth_challenge / auth_proof）的
 *   组装、识别均由上层 SecureClient 处理，Provider 不参与握手
 *
 * TOFU 首次连接（明文 auth）由 SecureClient 直接组装，不走 Provider。
 * PC 指派的识别码也由 SecureClient 走 Provider 外的 SealedBox 路径解封
 * （unsealAssignedPin）——此时双方还没有会话密钥，Provider 尚未可用。
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

/** 定长字节串比较（TOFU 的挑战 nonce 必须与 sealed 帧同源，见 SecureClient）。 */
function sameBytes(a, b) {
    if (!(a instanceof Uint8Array) || !(b instanceof Uint8Array)) return false;
    if (a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
    return diff === 0;
}

/**
 * 分片上传的请求认证 MAC（keyed BLAKE2b，docs/http-upload-design.md §5.3）。
 *
 * 与 Python 端 phonemic/tunnel/crypto/mac.py 同构：
 *     Python: hashlib.blake2b(msg, key=k, digest_size=32).digest()
 *     JS:     sodium.crypto_generichash(32, msg, k)
 *
 * ⚠️ 不要用 `sodium.crypto_auth` —— PyNaCl 1.6 的 nacl.bindings 里没有它，
 * 服务端一调用就 AttributeError。keyed BLAKE2b 才是两端都有的同一条构造。
 *
 * 被签内容固定为 `"PUT\n<sid>\n<offset>\n<len>"`：三项缺一不可——只签随机数的话，
 * 一份合法签名可以配上改过的偏移，去覆盖文件的别的位置。
 */
function uploadMac(kMac, sid, offset, len) {
    const msg = sodium.from_string('PUT\n' + sid + '\n' + offset + '\n' + len);
    return sodium.crypto_generichash(32, msg, kMac);
}

/** MAC 的线上形态：URL-safe base64、无 padding（与 Python 端 encode_mac 一致）。 */
function encodeMac(macBytes) {
    return sodium.to_base64(macBytes, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
}

/**
 * 密封 auth 数据（URL fragment 认证 / TOFU 重连路径）：
 * {"algo": <算法名>, "pk": <手机公钥 base64url>}
 * algo 只出现在密文内部（与 Python 端 KeyExchange.handle_auth 约定一致）
 */
function sealedAuthData(providerName, phonePublicKey, pcPublicKey) {
    const inner = JSON.stringify({
        algo: providerName,
        pk: sodium.to_base64(phonePublicKey, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
    });
    return sodium.crypto_box_seal(sodium.from_string(inner), pcPublicKey);
}

/**
 * 解密 TOFU 首次连接的 auth_challenge（SealedBox 加密）。
 * 返回 { pcPublicKey, nonce }，用于后续 ECDH + auth_proof。
 *
 * @param {Uint8Array} sealedBytes - SealedBox 密文
 * @param {Uint8Array} phonePrivateKey - 手机私钥原始字节
 */
function unsealTofuChallenge(sealedBytes, phonePrivateKey) {
    const phonePublicKey = sodium.crypto_scalarmult_base(phonePrivateKey);
    const inner = sodium.crypto_box_seal_open(
        sealedBytes, phonePublicKey, phonePrivateKey);
    const obj = JSON.parse(sodium.to_string(inner));
    return {
        pcPublicKey: sodium.from_base64(obj.pk, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
        nonce: sodium.from_base64(obj.nonce, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
    };
}

/**
 * 解封 PC 端 SealedBox 密封下发的 TOFU 识别码（第 2 步下行帧）。
 * 返回 { pin, nonce }：pin 大字显示给用户核对，nonce 必须与随后的
 * auth_challenge 同源（同一条连接），由上层校验。
 *
 * 识别码**由 PC 指派**，手机不生成也不回帧——因此它在明文链路上不可被抄走，
 * 也不存在用可控输入撞同一个码的空间（e2ee-always-on-design.md §5.5.1）。
 *
 * @param {Uint8Array} sealedBytes - SealedBox 密文
 * @param {Uint8Array} phonePrivateKey - 手机私钥原始字节
 */
function unsealAssignedPin(sealedBytes, phonePrivateKey) {
    const phonePublicKey = sodium.crypto_scalarmult_base(phonePrivateKey);
    const inner = sodium.crypto_box_seal_open(
        sealedBytes, phonePublicKey, phonePrivateKey);
    const obj = JSON.parse(sodium.to_string(inner));
    return {
        pin: obj.pin,
        nonce: sodium.from_base64(obj.nonce, sodium.base64_VARIANT_URLSAFE_NO_PADDING),
    };
}

/**
 * TOFU 首次连接的明文 auth 载荷。
 *
 * 首次连接没有 PC 公钥，无法密封，故两项均明文：
 * - algo：算法名。无信任锚时保密算法列表无安全意义，且服务端需要它来建 Provider
 * - pk：手机临时 X25519 公钥（32B）。公钥本身即公开值
 *
 * **不带识别码**：识别码改由 PC 指派、密封下发给这一方（unsealAssignedPin）。
 * 手机上不再存在任何「可复制、可重放」的识别码，抄走它这条路因此被堵死。
 */
function plaintextAuthData(providerName, phonePublicKey) {
    return {
        type: 'auth',
        algo: providerName,
        pk: phonePublicKey,          // Uint8Array，msgpack 编码为 bin
    };
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
    /** 获取手机公钥原始字节（TOFU 首次明文 auth 用）。 */
    get phonePublicKey() { return this._phonePublicKey; }
    /** 获取手机私钥原始字节（TOFU 首次解密 challenge 用）。 */
    get phonePrivateKey() { return this._phonePrivate; }
    /**
     * 设置 PC 公钥，并让已缓存的会话密钥**作废**。
     *
     * 作废是必须的：服务重启会换掉 PC 密钥对，重配对后 PC 公钥也随之改变。
     * 若 _sharedKey 仍留着上一任公钥派生的值，手机就会拿旧密钥去加密
     * auth_proof，服务端解不开——日志表现为「nonce mismatch」，且只有刷新
     * 页面才恢复。缓存键是会话密钥，它的输入变了就必须重算。
     */
    setPcPublicKey(rawBytes) {
        this._pcPublicKey = rawBytes;
        this._sharedKey = null;
    }
    /**
     * 直接注入一把对称密钥，跳过内部 ECDH。
     *
     * 用途：分片上传的 body 用 k_body 建一个**独立于 WS 那条流**的实例
     * （docs/http-upload-design.md §5.8）——seq 是单个计数器，两条独立 TCP 的到达
     * 顺序不由发送方决定，共用实例必然出现「必有一条解不开」的概率性失败。
     *
     * 上传密钥由服务端现生成、随 upload_ready 下发，不是 ECDH 协商来的，
     * 所以不能走 _deriveSharedKey。
     */
    injectSessionKey(rawBytes) {
        this._sharedKey = rawBytes;
    }
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
    /** 获取手机公钥原始字节（TOFU 首次明文 auth 用）。 */
    get phonePublicKey() { return this._phonePublicKey; }
    /** 获取手机私钥原始字节（TOFU 首次解密 challenge 用）。 */
    get phonePrivateKey() { return this._phonePrivate; }
    /**
     * 设置 PC 公钥，并让已缓存的会话密钥**作废**（理由同 NaClBoxProvider）。
     */
    setPcPublicKey(rawBytes) {
        this._pcPublicKey = rawBytes;
        this._sharedKey = null;
    }
    /**
     * 直接注入一把对称密钥，跳过内部 ECDH（理由见 NaClBoxProvider 同名方法）。
     *
     * ⚠️ 两种算法都**必须**有这个方法：协商优先级里 xchacha20 最高，上传实际几乎
     * 总是走它；少一个，`upload_ready` 之后的加密就当场 TypeError。
     */
    injectSessionKey(rawBytes) {
        this._sharedKey = rawBytes;
    }
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
        // seq 以 8 字节大端前缀焊入明文，与 NaClBoxProvider 完全同构
        // （docs/http-upload-design.md §5.8：两算法统一成同一条实现路径）。
        // 此前它走 AEAD 的 additional_data 槽位，两个算法因此分裂。
        const body = new Uint8Array(8 + plaintextBytes.length);
        body.set(seqToBytes(this._txSeq++), 0);
        body.set(plaintextBytes, 8);
        const ct = sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
            body, null, null, nonce, key);
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
        let body;
        try {
            // 签名：decrypt(secret_nonce, ciphertext, additional_data, public_nonce, key)
            body = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                null, ct, null, nonce, key);
        } catch (e) {
            throw new Error('[SEC] decrypt failed (MAC mismatch)');
        }
        // seq 在密文里（解密后才读得到）：与 XSalsa20 同一条判据
        const seq = seqFromBytes(body.slice(0, 8));
        if (seq !== this._rxSeq) {
            throw new Error('[SEC] replay detected (seq ' + seq + ' != expected ' + this._rxSeq + ')');
        }
        this._rxSeq++;
        return body.slice(8);
    }
    reset() {
        this._txSeq = 0;
        this._rxSeq = 0;
    }
}

const PROVIDER_CLASSES = {
    'xsalsa20': NaClBoxProvider,
    'xchacha20': XChaCha20Provider,
};
