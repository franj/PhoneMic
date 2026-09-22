"""
隧道模式管理。
管理 LAN / Cloudflare 模式的切换和配置持久化。
"""

from enum import Enum
from typing import Optional

from phonemic.utils.settings_manager import SettingsManager


class TunnelMode(str, Enum):
    """服务端绑定模式。"""
    LAN = "lan"
    CLOUDFLARE = "cloudflare"


def get_mode() -> TunnelMode:
    """从配置读取当前模式，默认 LAN。"""
    sm = SettingsManager.instance()
    value = sm.get("tunnel_mode", "lan")
    try:
        return TunnelMode(value)
    except ValueError:
        return TunnelMode.LAN


def set_mode(mode: TunnelMode) -> None:
    """保存模式到配置。"""
    sm = SettingsManager.instance()
    sm.set("tunnel_mode", mode.value)


def effective_auth_method(auth_method: str, mode: TunnelMode) -> str:
    """根据当前模式返回最终生效的认证方式。

    返回值为 "tofu"（手动审批）或 "url_fragment"（扫码认证）。
    Cloudflare 公网可达，TOFU 首次连接无信任锚，强制 url_fragment。
    LAN 模式下尊重用户选择。
    """
    if mode == TunnelMode.CLOUDFLARE:
        return "url_fragment"
    return auth_method
