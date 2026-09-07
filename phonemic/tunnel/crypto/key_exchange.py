"""算法无关的密钥交换：SealedBox 解封 -> 读 algo + 手机公钥 -> ECDH -> KDF。

本类不绑定任何具体对称算法，因此可以先于 Provider 实例化：先解 auth 拿到
algo，再据此建对应 CryptoProvider（解决"先有 Provider 才能解 auth、解了 auth
才知道建哪个 Provider"的死锁）。

PC 身份私钥由 SecureChannel 在进程生命周期内稳定持有（每次建连重新生成，
只通过二维码带外分发，等价于带外 token）。手机临时私钥从不出手机，会话密钥
只有做过密钥交换的两方持有。
"""

import base64
import json
from hashlib import blake2b
from typing import List, Tuple

from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.tunnel.crypto.errors import CryptoError


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


class KeyExchange:
    """PC 端密钥交换：持有长期 X25519 身份私钥，处理手机发来的 auth。"""

    def __init__(self, pc_private: PrivateKey, allowed_algos: List[str]):
        self._pc_private = pc_private
        self._allowed = set(allowed_algos)

    @property
    def public_key_b64(self) -> str:
        """用于二维码 fragment 的 PC 公钥（base64url 无 padding）。"""
        return _to_b64(bytes(self._pc_private.public_key))

    @property
    def private_key(self) -> PrivateKey:
        return self._pc_private

    def handle_auth(self, sealed_data: bytes) -> Tuple[str, bytes]:
        """解封手机发来的 auth.data，返回 ``(algo, session_key)``。

        ``sealed_data`` 解封后是 JSON：``{"algo": <算法名>, "pk": <手机公钥
        base64url>}``。algo 取自密文内部（非明文），故本方法无需调用方先告知
        algo——这正是把它从 Provider 里抽出来的原因。

        Args:
            sealed_data: 已 base64 解码的 SealedBox 密文（手机用 PC 公钥密封）。

        Returns:
            ``(algo, session_key)``：algo 为协商出的对称算法名，session_key 为
            32 字节会话密钥（ECDH + BLAKE2b KDF 派生）。

        Raises:
            CryptoError: 解封失败、内部格式错误、或 algo 不在允许列表。
        """
        try:
            inner = SealedBox(self._pc_private).decrypt(sealed_data)
            d = json.loads(inner)
            algo = d["algo"]
            pk_b64 = d["pk"]
            if algo not in self._allowed:
                raise ValueError(f"algorithm '{algo}' not allowed")
            phone_public = PublicKey(_from_b64(pk_b64))
            shared = crypto_scalarmult(bytes(self._pc_private), bytes(phone_public))
            session_key = blake2b(shared, digest_size=32).digest()
            return algo, session_key
        except CryptoError:
            raise
        except Exception as e:
            raise CryptoError(f"key exchange failed: {e}") from e
