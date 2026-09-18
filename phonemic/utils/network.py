import os
import re
import netifaces
import psutil
from ipaddress import ip_address, ip_network
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
import socket

# psutil 只导出了 AF_LINK（Mac 地址那一类），IPv4 的地址族要用 socket 里的
_AF_INET = socket.AF_INET

@dataclass
class IpCandidate:
    ip: str                # 例如 "192.168.1.100"
    interface_name: str    # 网卡名称，如 "以太网" 或 "Wi-Fi"
    description: str       # 网卡描述（如 "Realtek PCIe GbE Family Controller"）
    priority: float        # 数字越小越优先，由算法计算
    is_virtual: bool = False
    is_default_gateway_match: bool = False
    interface_type: str = "unknown"  # "wifi", "ethernet", "virtual", "other"
    metric: int = 0        # 跃点数
    mac: str = ""          # MAC 地址（如 "00-11-22-33-44-55"）


# ---------- 内部辅助函数 ----------

def _get_interface_info(iface_name: str) -> Dict[str, Any]:
    """
    获取网卡的详细信息（是否启用、MAC地址等）。
    跃点数(metric)因获取复杂，本实现暂不返回，优先级算法不使用跃点数。
    """
    info = {
        "is_up": False,
        "mac": "",
        "description": iface_name,
    }
    try:
        stats = psutil.net_if_stats().get(iface_name)
        if stats:
            info["is_up"] = stats.isup
            if stats.mtu:
                # MTU 可用作网卡存在的标志
                pass
        addrs = psutil.net_if_addrs().get(iface_name, [])
        for addr in addrs:
            if addr.family == psutil.AF_LINK:   # MAC 地址
                info["mac"] = addr.address
                break
        # 尝试获取网卡描述（Windows下psutil通常返回友好名称）
        info["description"] = iface_name
    except Exception:
        # 出现任何异常，保持默认值（is_up=False）
        pass
    return info


def _is_virtual_interface(desc: str) -> bool:
    """根据网卡名判断是否为虚拟网卡（只用于降权，不排除）。

    比对的是 psutil 的友好名，Windows 上可能是中文（「本地连接* 1」）或
    「vEthernet (Default Switch)」这类，所以关键词要覆盖中英两套写法。
    """
    pattern = (
        r"(VMware|VirtualBox|Hyper-V|vEthernet|Virtual|VBox|VMnet|"
        r"Npcap|TAP-Windows|Wintun|WireGuard|OpenVPN|Tailscale|ZeroTier|Radmin|"
        r"Loopback|Bluetooth|蓝牙|虚拟|"
        r"本地连接\s*\*|Local Area Connection\s*\*)"
    )
    return bool(re.search(pattern, desc, re.IGNORECASE))


def _guess_interface_type(name: str, desc: str = "") -> str:
    """
    猜测网卡类型：wifi / ethernet / other，只影响优先级。

    同样比对 psutil 的友好名：Windows 中文系统上是「WLAN」「以太网」，
    英文系统上是「Wi-Fi」「Ethernet」，所以中英关键词都要有，且 Wi-Fi 的
    英文名带连字符（「Wi-Fi」），不能只匹配 "wifi"。
    """
    combined = f"{name} {desc}".lower()
    if re.search(r"(wi-?fi|wireless|wlan|无线)", combined):
        return "wifi"
    if re.search(
        r"(ethernet|gigabit|pcie|realtek|intel.*ether|以太网|"
        r"本地连接(?!\s*\*)|Local Area Connection(?!\s*\*))",
        combined,
    ):
        return "ethernet"
    return "other"


def _get_default_gateway_ip() -> Optional[str]:
    """获取默认网关的 IPv4 地址，若无则返回 None"""
    try:
        gateways = netifaces.gateways()
        # gateways['default'] 结构: {2: (gateway_ip, iface_name)}
        default = gateways.get('default', {})
        if netifaces.AF_INET in default:
            gateway_ip = default[netifaces.AF_INET][0]
            return gateway_ip
    except Exception:
        pass
    return None


def _is_same_subnet(ip: str, gateway_ip: str, netmask: str) -> bool:
    """判断 IP 与网关是否在同一子网（通过 IP 和 netmask 计算）"""
    try:
        # 使用 ipaddress 库计算网络地址
        ip_obj = ip_address(ip)
        net = ip_network(f"{gateway_ip}/{netmask}", strict=False)
        # 检查 ip 是否属于该网络
        return ip_obj in net
    except Exception:
        # 若计算失败（例如 netmask 格式错误），返回 False
        return False

def get_network_interface_name_by_ip(ip_address: str) -> Optional[str]:
    """按 IP 反查 psutil 的网卡友好名（get_all_lan_ips 已不再依赖它）。"""
    ip_address = ip_address.strip()  # 去除多余空格
    for interface_name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            # addr.address 包含 IP 地址字符串，直接比较
            if addr.address == ip_address:
                return interface_name
    return None

def get_all_lan_ips() -> List[IpCandidate]:
    """枚举本机可用的局域网 IPv4 候选，按 priority 升序（越小越优先）。

    网卡枚举**只用 psutil 一套命名空间**：netifaces 在 Windows 上返回适配器
    GUID（``{DE30760F-...}``），psutil 返回友好名（``WLAN`` / ``以太网``），
    两者**没有交集**。原实现拿 netifaces 的名字去查 psutil 的表，导致：

    - MAC 恒为空 → ``last`` 网络选择模式永远匹配不上、只能回退 ``auto``；
    - ``net_if_stats`` 查不到 → 未启用的网卡判不出来；
    - 网卡类型猜不出来（GUID 里没有 wifi/ethernet 关键词）→ 优先级全凭运气。

    netifaces 现在只保留给 ``_get_default_gateway_ip()``（psutil 没有网关 API）。

    虚拟网卡只**降权不排除**：用户可能只有一块虚拟网卡可用，排在后面总比
    直接选不出来强。
    """
    default_gateway = _get_default_gateway_ip()
    stats = psutil.net_if_stats()
    candidates = []

    for iface_name, addrs in psutil.net_if_addrs().items():
        iface_stats = stats.get(iface_name)
        if iface_stats is not None and not iface_stats.isup:
            continue   # 跳过未启用的网卡

        # MAC 与 IP 取自同一张表（同一个键），命名空间天然一致
        mac = ""
        for addr in addrs:
            if addr.family == psutil.AF_LINK:
                mac = addr.address or ""
                break

        # 判断接口类型和是否虚拟
        iface_type = _guess_interface_type(iface_name)
        is_virtual = _is_virtual_interface(iface_name)

        for addr in addrs:
            if addr.family != _AF_INET:
                continue
            ip = addr.address
            # 过滤回环和链路本地（169.254.x.x 是自动私有地址，连不上对端）
            if not ip or ip.startswith('127.') or ip.startswith('169.254.'):
                continue

            netmask = addr.netmask
            gateway_match = bool(
                default_gateway and netmask
                and _is_same_subnet(ip, default_gateway, netmask)
            )

            # 计算优先级权重
            weight = 0
            if not is_virtual:
                weight += 100
            if gateway_match:
                weight += 50
            if iface_type == 'wifi':
                weight += 30
            elif iface_type == 'ethernet':
                weight += 20
            # 跃点数暂无法可靠获取，忽略
            priority = -weight   # 因为 priority 越小越优先，所以取负权重

            candidates.append(IpCandidate(
                ip=ip,
                interface_name=iface_name,
                description=iface_name,   # Windows 下 psutil 的键就是友好名，直接展示
                priority=priority,
                is_virtual=is_virtual,
                is_default_gateway_match=gateway_match,
                interface_type=iface_type,
                metric=0,
                mac=mac   # 传入 MAC
            ))
    
    # 按 priority 升序排序
    candidates.sort(key=lambda c: c.priority)
    return candidates

def get_best_ip(candidates: Optional[List[IpCandidate]] = None) -> Optional[str]:
    if candidates is None:
        candidates = get_all_lan_ips()
    if not candidates:
        return None
    return candidates[0].ip

def get_local_ip() -> Optional[str]:
    return get_best_ip()

def find_free_port(start_port: int = 12000, max_tries: int = 100, host: str = "") -> Optional[int]:
    """
    从 start_port 开始向后查找第一个可绑定的 TCP 端口。

    host 必须传服务端实际要绑定的地址。Windows 允许 (0.0.0.0, P) 与
    (192.168.x.x, P) 同时存在，若用空串（0.0.0.0）探测，在「具体 IP 上
    该端口已被占用」时会被误判为空闲，导致后续真正 bind 时报 10048。
    SO_REUSEADDR 只在非 Windows 设置，与 asyncio.create_server 的默认行为一致。
    """
    reuse_address = os.name != "nt"
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                if reuse_address:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
            except OSError:
                continue
    return None

def find_candidate_by_mac(mac: str, candidates: List[IpCandidate]) -> Optional[IpCandidate]:
    """
    根据 MAC 地址（不区分大小写）查找匹配的候选。
    若未找到返回 None。
    """
    mac_lower = mac.lower()
    for c in candidates:
        if c.mac.lower() == mac_lower:
            return c
    return None