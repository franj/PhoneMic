"""加密层异常。

解密失败与重放区分成两类异常，供上层（SecureSession）映射为 error.code。
安全上两类对客户端行为一致（均视为"该帧不可用"），不暴露是篡改还是重放。
"""


class CryptoError(Exception):
    """加密层异常基类。"""


class DecryptError(CryptoError):
    """MAC 校验失败：密钥错误、密文被篡改，或（AAD 路径）seq 不递增。

    AAD 路径（XChaCha20 / AES-GCM）下 seq 作为 aad 传入，seq 不对时
    AEAD 整体校验失败，表现与"密文被篡改"相同，一律归此类。
    """


class ReplayError(CryptoError):
    """seq 不递增（仅前缀路径 XSalsa20 能明确区分）。

    前缀路径把 seq(8B) 前置到明文后加密，解密成功即可读 seq，
    此时能明确判断是"重放/乱序"而非"篡改"。外部仍按统一策略
    （"重放当解密失败处理"）与 DecryptError 同等对待。
    """
