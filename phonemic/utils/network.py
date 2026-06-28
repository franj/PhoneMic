import re
import netifaces
import psutil
from ipaddress import ip_address, ip_network
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
import socket

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
    """根据网卡描述判断是否为虚拟网卡"""
    pattern = r"(VMware|VirtualBox|Hyper-V|Virtual|VBox|VMnet)"
    return bool(re.search(pattern, desc, re.IGNORECASE))


def _guess_interface_type(name: str, desc: str) -> str:
    """
    猜测网卡类型：wifi / ethernet / other
    基于名称或描述中的关键词。
    """
    combined = f"{name} {desc}".lower()
    if re.search(r"(wifi|wireless|wlan)", combined):
        return "wifi"
    if re.search(r"(ethernet|gigabit|pcie|realtek|intel.*ether)", combined):
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
    ip_address = ip_address.strip()  # 去除多余空格
    for interface_name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            # addr.address 包含 IP 地址字符串，直接比较
            if addr.address == ip_address:
                return interface_name
    return None

def get_all_lan_ips() -> List[IpCandidate]:
    candidates = []
    default_gateway = _get_default_gateway_ip()
    
    for iface_name in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface_name)
        if netifaces.AF_INET not in addrs:
            continue
        # 获取网卡描述（Windows 下 netifaces 可能不直接提供，需用 cc）
        try:
            description = get_network_interface_name_by_ip(addrs[netifaces.AF_INET][0]['addr'])  # psutil 返回的是友好名称
            if_stats = psutil.net_if_stats().get(description)
            if not if_stats.isup:
                continue   # 跳过未启用的网卡
        except:
            description = iface_name
        
        # 判断接口类型和是否虚拟
        iface_type = _guess_interface_type(iface_name, description)
        is_virtual = _is_virtual_interface(description)
        
        # 获取 MAC 地址
        mac = ""
        try:
            addrs_mac = psutil.net_if_addrs().get(iface_name, [])
            for addr in addrs_mac:
                if addr.family == psutil.AF_LINK:
                    mac = addr.address
                    break
        except Exception:
            pass  # 保持为空
        
        for addr in addrs[netifaces.AF_INET]:
            ip = addr['addr']
            netmask = addr.get('netmask')
            # 过滤回环和链路本地
            if ip.startswith('127.') or ip.startswith('169.254.'):
                continue
            # 计算优先级权重
            weight = 0
            if not is_virtual:
                weight += 100
            if default_gateway and netmask and _is_same_subnet(ip, default_gateway, netmask):
                weight += 50
            if iface_type == 'wifi':
                weight += 30
            elif iface_type == 'ethernet':
                weight += 20
            # 跃点数暂无法获取，忽略
            priority = -weight   # 因为 priority 越小越优先，所以取负权重
            
            candidates.append(IpCandidate(
                ip=ip,
                interface_name=iface_name,
                description=description,
                priority=priority,
                is_virtual=is_virtual,
                is_default_gateway_match=(default_gateway and netmask and _is_same_subnet(ip, default_gateway, netmask)),
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

def find_free_port(start_port: int = 12000, max_tries: int = 100) -> Optional[int]:
    """
    Finds an available TCP port starting from `start_port`.
    """
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                # Set socket option to allow reuse of the address
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("", port))
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