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


def get_bind_address(mode: TunnelMode) -> str:
    """返回模式对应的绑定地址。"""
    if mode == TunnelMode.CLOUDFLARE:
        return "127.0.0.1"
    return "0.0.0.0"


def effective_algorithm(algo: str, mode: TunnelMode) -> str:
    """根据当前模式返回最终生效的加密设置。

    返回值为 "none"（不加密）或 "auto"（加密，具体算法由客户端协商）。
    历史配置值 xsalsa20/xchacha20 一律归一化为 auto。
    Cloudflare 模式下明文传输存在互联网中间人风险，
    即使用户配置为 none，也强制加密，配置保持原值不修改。
    """
    algo = "none" if algo == "none" else "auto"
    if mode == TunnelMode.CLOUDFLARE and algo == "none":
        return "auto"
    return algo
