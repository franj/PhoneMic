"""消息帧编解码：MessagePack。

本模块是协议编码层的唯一实现（wire-protocol.md 第 3 节）。加密层的输入输出
都是字节，对这里选什么编码完全不感知（crypto-design.md §1），因此换掉
MessagePack（→ CBOR 等）只需改本文件。

对外只有两个纯函数，不碰网络、不依赖 WebSocket，可完整进 pytest：

    encode(frame: dict) -> bytes
    decode(raw: bytes) -> dict

约定：
  - 编码一律 ``use_bin_type=True``——bytes 走 msgpack 原生 ``bin`` 类型，
    因此帧里不再需要 base64 包裹密文（这正是选 msgpack 而非 JSON 的原因之一）
  - 解码一律 ``raw=False``——键为 str，保持默认严格模式（非 str 键直接报错），
    避免 msgpack-python 1.0 前后默认值差异导致的键类型漂移
"""

from typing import Any, Dict

import msgpack

__all__ = ["FrameError", "encode", "decode"]


class FrameError(ValueError):
    """帧不是合法的 msgpack map（解码失败，或解出来的不是 map）。"""


def encode(frame: Dict[str, Any]) -> bytes:
    """把消息帧编码为 msgpack 字节。

    Args:
        frame: 消息帧字典，键必须为 str。

    Returns:
        msgpack 编码后的字节串，可直接作为 WS binary 帧载荷。
    """
    return msgpack.packb(frame, use_bin_type=True)


def decode(raw: bytes) -> Dict[str, Any]:
    """把 msgpack 字节解码为消息帧。

    Args:
        raw: WS binary 帧的原始字节。

    Returns:
        解码出的消息帧字典。

    Raises:
        FrameError: 不是合法 msgpack，或解出来不是 map（如客户端发来裸数组）。
    """
    try:
        frame = msgpack.unpackb(raw, raw=False)
    except Exception as e:
        raise FrameError(f"invalid msgpack frame: {e}") from e
    if not isinstance(frame, dict):
        raise FrameError(f"frame must be a map, got {type(frame).__name__}")
    return frame
