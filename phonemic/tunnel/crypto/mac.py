"""请求级 MAC 原语（keyed BLAKE2b）。

用途：HTTP 分片上传的请求认证（``X-Pm-Mac``，docs/http-upload-design.md §5.3）。
它只证明「这串数据是我发的、没被动过」，不隐藏内容——内容的机密性与完整性由
body 的 AEAD 负责。

⚠️ 为什么是 keyed BLAKE2b 而不是 libsodium 的 ``crypto_auth``

PyNaCl 1.6 的 ``nacl.bindings`` 里**没有** ``crypto_auth``（``crypto_onetimeauth``
也没有），只有 JS 侧（sodium.js）有。照 ``crypto_auth`` 写，服务端一调用就
``AttributeError``。keyed BLAKE2b 是两端都有的同一条 libsodium 构造，而且本项目
本来就用它做 KDF（``key_exchange.py`` / ``crypto_providers.js``），换上去零新增依赖，
也不会出现两端算法不一致。

两端同构：

    Python: hashlib.blake2b(msg, key=k, digest_size=32).digest()
    JS:     sodium.crypto_generichash(32, msg, k)
"""

from __future__ import annotations

import base64
from hashlib import blake2b
from typing import Optional

# 与 JS 侧 sodium.crypto_generichash(32, ...) 的输出长度一致
MAC_SIZE = 32

# 签名覆盖的方法名：把方法钉进被签内容，防止一份签名被挪用到别的请求上
MAC_METHOD = "PUT"


def keyed_blake2b(key: bytes, message: bytes, size: int = MAC_SIZE) -> bytes:
    """keyed BLAKE2b：等价于 ``sodium.crypto_generichash(size, message, key)``。"""
    return blake2b(message, key=key, digest_size=size).digest()


def upload_message(sid: str, offset: int, length: int) -> bytes:
    """上传片的被签内容：``"PUT\\n<sid>\\n<offset>\\n<len>"``。

    三项缺一不可（§5.3）：只签随机数的话，攻击者能把合法签名配上改过的偏移，
    去覆盖文件的别的位置。
    """
    return f"{MAC_METHOD}\n{sid}\n{offset}\n{length}".encode("utf-8")


def upload_mac(k_mac: bytes, sid: str, offset: int, length: int) -> bytes:
    """算出上传片的 MAC（32 字节原始值）。"""
    return keyed_blake2b(k_mac, upload_message(sid, offset, length))


def encode_mac(mac: bytes) -> str:
    """线上形态：URL-safe base64、无 padding（与项目其余线上字节一致）。"""
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def decode_mac(raw: Optional[str]) -> Optional[bytes]:
    """解析 ``X-Pm-Mac`` 头；格式不合法返回 None（调用方按「拿不出凭证」处理）。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # 补回被剥掉的 padding（长度不合法时 b64decode 会抛 ValueError）
        padded = raw + "=" * (-len(raw) % 4)
        mac = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    if len(mac) != MAC_SIZE:
        return None
    return mac
