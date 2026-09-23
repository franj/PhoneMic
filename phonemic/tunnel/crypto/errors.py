"""加密层异常。

解密失败与重放区分成两类异常，供上层（SecureSession）映射为 error.code。
安全上两类对客户端行为一致（均视为"该帧不可用"），不暴露是篡改还是重放。
"""


class CryptoError(Exception):
    """加密层异常基类。"""


class DecryptError(CryptoError):
    """MAC 校验失败：密钥错误、密文（或 nonce）被篡改。

    两种算法的密文布局统一为 ``nonce(24) ‖ AEAD(seq(8) ‖ 明文) ‖ tag(16)``。
    tag 覆盖的是「nonce ‖ 密文」而非明文，所以**解密成功并不代表 seq 正确** ——
    seq 要解密后才读得到，比对失败归 `ReplayError`（见下），不归此类。
    """


class ReplayError(CryptoError):
    """seq 不等于期望值（重放 / 乱序 / 跳号）。

    seq 被焊成明文前 8 字节，解密成功即可读出并比对，因此「序号不对」能明确
    区分于「篡改」；XSalsa20 与 XChaCha20 走的是同一条判定。
    外部仍按统一策略（"重放当解密失败处理"）与 DecryptError 同等对待。
    """
