"""
测试 phonemic.utils.network 模块
覆盖：
- get_all_lan_ips() 的候选 IP 获取与优先级排序
- get_local_ip() 在有/无可用 IP 时的返回值
- 边缘场景：无网络、仅虚拟网卡、多网卡优先级
- MAC 地址字段和 find_candidate_by_mac 函数
"""

import pytest
import psutil
from unittest.mock import Mock, patch, MagicMock
from phonemic.utils import network
from phonemic.utils.network import get_all_lan_ips, get_local_ip, IpCandidate, find_free_port, find_candidate_by_mac
import socket

def test_get_all_lan_ips_returns_list():
    candidates = get_all_lan_ips()
    assert isinstance(candidates, list)
    
def test_get_all_lan_ips_contains_mac():
    """每个候选都带 mac 字段；有网卡时至少一个非空。

    断言 mac 非空是必须的——last 网络选择模式全靠它匹配（见下方
    TestGetAllLanIpsMocked::test_mac_comes_from_the_same_table_as_ip）。
    """
    candidates = get_all_lan_ips()
    if not candidates:
        pytest.skip("本机没有可用 LAN IP")
    for c in candidates:
        assert hasattr(c, 'mac')
    assert any(c.mac for c in candidates), "有候选却全都没有 MAC：last 模式会永远回退 auto"

def test_find_candidate_by_mac():
    """测试通过 MAC 查找候选"""
    c1 = IpCandidate("192.168.1.1", "eth0", "Ethernet", 0, mac="00:11:22:33:44:55")
    c2 = IpCandidate("10.0.0.1", "wlan0", "Wi-Fi", 1, mac="AA:BB:CC:DD:EE:FF")
    candidates = [c1, c2]
    
    # 精确匹配
    found = find_candidate_by_mac("00:11:22:33:44:55", candidates)
    assert found is c1
    
    # 不区分大小写
    found = find_candidate_by_mac("aa:bb:cc:dd:ee:ff", candidates)
    assert found is c2
    
    # 未找到
    found = find_candidate_by_mac("00:00:00:00:00:00", candidates)
    assert found is None

# ========== 测试 find_free_port ==========
def test_find_free_port_finds_a_port():
    """测试函数能找到一个可用的端口"""
    host = "127.0.0.1"
    port = find_free_port(start_port=9000, host=host)
    assert isinstance(port, int)
    assert port >= 9000
    # 验证端口在该地址上确实可用
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.bind((host, port)) is None  # bind 成功时返回 None

def test_find_free_port_skips_port_in_use_on_specific_ip():
    """回归：端口被「具体 IP」占用时必须跳过。

    旧实现用 ("", port) 探测，Windows 下 (0.0.0.0, P) 与 (具体IP, P)
    可以共存，于是被占用的端口仍被判为空闲，真正 bind 时才报 10048。
    """
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind((host, 0))
        used_port = holder.getsockname()[1]
        holder.listen(1)
        found = find_free_port(start_port=used_port, max_tries=20, host=host)

    assert found is not None
    assert found != used_port
    assert found > used_port

def test_find_free_port_skips_used_port(mocker):
    """当起始端口被占用时，应返回下一个可用端口"""
    used_port = 9100

    def bind_side_effect(address):
        if address[1] == used_port:
            raise OSError("Address already in use")
        return None  # Simulate successful bind for other ports

    mocker.patch('socket.socket.bind', side_effect=bind_side_effect)

    free_port = find_free_port(start_port=used_port)
    assert free_port == used_port + 1

def test_find_free_port_returns_none_if_all_are_used(mocker):
    """如果范围内的所有端口都被占用，应返回 None"""
    # 模拟所有 bind 调用都失败
    mocker.patch('socket.socket.bind', side_effect=OSError("Address already in use"))
    
    port = find_free_port(start_port=9200, max_tries=5)
    assert port is None


def test_get_local_ip_returns_string_or_none():
    ip = get_local_ip()
    assert ip is None or isinstance(ip, str)


# ========== Fixtures 辅助函数 ==========
def create_mock_interface(name: str, ips: list, is_virtual: bool = False, is_wifi: bool = False):
    """
    创建模拟的 netifaces 接口数据
    Args:
        name: 接口名称，如 '以太网', 'Wi-Fi', 'VMware'
        ips: IPv4 地址列表，每个元素为 {'addr': '192.168.1.100', 'netmask': '255.255.255.0'}
        is_virtual: 是否为虚拟网卡（VMware/VirtualBox）
        is_wifi: 是否为无线网卡（用于优先级加分）
    Returns:
        适合 netifaces.ifaddresses 返回格式的字典片段
    """
    # 实际 netifaces 返回的数据结构复杂，我们简化模拟
    # 这里返回一个简单的可迭代对象，用于测试逻辑
    return {
        'name': name,
        'is_virtual': is_virtual,
        'is_wifi': is_wifi,
        'addrs': ips
    }


# ========== 测试 get_local_ip ==========
def test_get_local_ip_returns_best_ip():
    """
    有可用 IP 时，get_local_ip 应返回优先级最高的 IP（即 get_all_lan_ips 排序后的第一个）
    """
    # 注意：需要添加 mac 字段（可为空）
    mock_candidates = [
        IpCandidate("192.168.1.100", "Wi-Fi", "Wi-Fi", priority=0, mac=""),
        IpCandidate("10.0.0.2", "Ethernet", "Ethernet", priority=1, mac=""),
    ]
    with patch('phonemic.utils.network.get_all_lan_ips', return_value=mock_candidates):
        ip = get_local_ip()
        assert ip == "192.168.1.100"


def test_get_local_ip_no_network_returns_none():
    """
    无可用 IP 时，get_local_ip 应返回 None
    """
    with patch('phonemic.utils.network.get_all_lan_ips', return_value=[]):
        ip = get_local_ip()
        assert ip is None


def test_get_local_ip_single_candidate():
    """
    只有一个候选时，直接返回该 IP
    """
    mock_candidates = [
        IpCandidate("192.168.1.105", "Wi-Fi", "Wi-Fi", priority=0, mac=""),
    ]
    with patch('phonemic.utils.network.get_all_lan_ips', return_value=mock_candidates):
        ip = get_local_ip()
        assert ip == "192.168.1.105"


# ========== get_all_lan_ips 的网卡枚举 ==========
#
# 回归背景：真实 Windows 上 netifaces.interfaces() 返回适配器 GUID
# （"{DE30760F-...}"），而 psutil.net_if_addrs() 的键是友好名（"WLAN" / "以太网"），
# 两套命名空间**没有交集**。旧实现拿 netifaces 的名字去查 psutil 的表，于是
# MAC 恒为空（last 模式永远匹配不上）、isup 判不出来、网卡类型也猜不出来。
#
# 下面每个用例都按这个真实形态造数据，且 netifaces 一侧只返回 GUID、
# ifaddresses() 返回空字典 —— 一旦有人改回按 netifaces 枚举，候选会直接变空，
# 断言立刻失败，而不是静默取到空 MAC。

_GUID_A = "{DE30760F-EADB-477B-AF65-E236B4C0FE34}"
_GUID_B = "{950FE754-8D68-474E-92EE-78DF8FDE3459}"


class _Addr:
    """psutil.net_if_addrs() 里的一个地址项替身。"""

    def __init__(self, family, address, netmask=None):
        self.family = family
        self.address = address
        self.netmask = netmask


class _IfStats:
    def __init__(self, isup=True):
        self.isup = isup


def _iface(mac, ip, netmask="255.255.255.0"):
    """一张网卡的 psutil 地址表：一个 AF_LINK（MAC）+ 一个 IPv4。"""
    return [_Addr(psutil.AF_LINK, mac), _Addr(socket.AF_INET, ip, netmask)]


def _patch_interfaces(monkeypatch, addrs, stats=None, gateway_ip="192.168.5.1"):
    """把网卡枚举替换成可控数据。"""
    monkeypatch.setattr(network.psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(network.psutil, "net_if_stats", lambda: stats or {})
    monkeypatch.setattr(network.netifaces, "interfaces", lambda: [_GUID_A, _GUID_B])
    monkeypatch.setattr(network.netifaces, "ifaddresses", lambda name: {})
    if gateway_ip is None:
        monkeypatch.setattr(network.netifaces, "gateways", lambda: {})
    else:
        monkeypatch.setattr(
            network.netifaces, "gateways",
            lambda: {"default": {network.netifaces.AF_INET: (gateway_ip, _GUID_A)}},
        )


class TestGetAllLanIpsMocked:
    def test_mac_comes_from_the_same_table_as_ip(self, monkeypatch):
        """MAC 必须取到（修复前恒为空 → last 模式永远回退 auto）。"""
        _patch_interfaces(monkeypatch, {
            "WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49"),
        })

        cands = get_all_lan_ips()

        assert len(cands) == 1
        c = cands[0]
        assert c.mac == "E4-C7-67-15-BF-08"      # 修复前 == ""
        assert c.interface_name == "WLAN"        # 修复前是 netifaces 的 GUID
        assert c.description == "WLAN"
        assert c.interface_type == "wifi"        # 修复前 GUID 里没关键词 → other
        assert c.is_virtual is False

    def test_last_mode_mac_roundtrip(self, monkeypatch):
        """last 模式的闭环：候选里的 MAC 必须能被 find_candidate_by_mac 找回。"""
        _patch_interfaces(monkeypatch, {
            "WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49"),
            "以太网": _iface("AA-BB-CC-DD-EE-01", "10.0.0.7"),
        })

        cands = get_all_lan_ips()
        saved = next(c.mac for c in cands if c.ip == "10.0.0.7")

        assert saved, "候选必须带 MAC，否则 last 模式无法工作"
        assert find_candidate_by_mac(saved, cands).ip == "10.0.0.7"

    def test_down_adapter_is_skipped(self, monkeypatch):
        """未启用的网卡不参与选择（修复前 net_if_stats 查不到，判不出来）。"""
        _patch_interfaces(
            monkeypatch,
            {
                "WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49"),
                "以太网": _iface("AA-BB-CC-DD-EE-01", "10.0.0.7"),
            },
            stats={"WLAN": _IfStats(isup=True), "以太网": _IfStats(isup=False)},
        )

        assert [c.ip for c in get_all_lan_ips()] == ["192.168.5.49"]

    def test_loopback_and_link_local_are_filtered(self, monkeypatch):
        _patch_interfaces(monkeypatch, {
            "Loopback Pseudo-Interface 1": [_Addr(socket.AF_INET, "127.0.0.1", "255.0.0.0")],
            "本地连接* 1": [_Addr(socket.AF_INET, "169.254.10.20", "255.255.0.0")],
            "WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49"),
        })

        assert [c.ip for c in get_all_lan_ips()] == ["192.168.5.49"]

    def test_adapter_without_ipv4_is_ignored(self, monkeypatch):
        """只有 MAC、没有 IPv4 的网卡不产生候选（虚拟交换机常见）。"""
        _patch_interfaces(monkeypatch, {
            "vEthernet (Default Switch)": [_Addr(psutil.AF_LINK, "00-15-5D-00-00-01")],
            "WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49"),
        })

        assert [c.ip for c in get_all_lan_ips()] == ["192.168.5.49"]

    def test_virtual_adapter_ranks_last(self, monkeypatch):
        """虚拟网卡即使命中默认网关，也要排在有线网卡后面。

        权重：物理以太网 = 100（非虚拟）+ 20（ethernet）= 120；
        Hyper-V 虚机网卡 = 0（虚拟）+ 50（网关同网段）+ 20 = 70。
        修复前虚拟判定失效 → 虚机网卡反而会排到前面。
        """
        _patch_interfaces(monkeypatch, {
            "Hyper-V Virtual Ethernet Adapter": _iface("00-15-5D-00-00-01", "192.168.5.50"),
            "以太网": _iface("AA-BB-CC-DD-EE-01", "10.0.0.7"),
        })

        cands = get_all_lan_ips()

        assert [c.ip for c in cands] == ["10.0.0.7", "192.168.5.50"]
        assert cands[0].is_virtual is False
        assert cands[1].is_virtual is True

    def test_gateway_match_outranks_interface_type(self, monkeypatch):
        """网关同网段（能直连对端）优先于网卡类型。"""
        _patch_interfaces(monkeypatch, {
            "WLAN": _iface("E4-C7-67-15-BF-08", "10.0.0.7"),
            "以太网": _iface("AA-BB-CC-DD-EE-01", "192.168.5.49"),
        })

        # 网关 192.168.5.1：以太网 100+50+20=170 > WLAN 100+30=130
        assert get_all_lan_ips()[0].ip == "192.168.5.49"

    def test_no_gateway_information(self, monkeypatch):
        """拿不到网关时退化为按网卡类型排序，不应报错。"""
        _patch_interfaces(monkeypatch, {"WLAN": _iface("E4-C7-67-15-BF-08", "192.168.5.49")},
                          gateway_ip=None)

        cands = get_all_lan_ips()
        assert len(cands) == 1
        assert cands[0].is_default_gateway_match is False


class TestInterfaceHeuristics:
    """网卡名分类 / 虚拟判定：必须覆盖 Windows 中文友好名与带连字符的 Wi-Fi。"""

    @pytest.mark.parametrize("name,expected", [
        ("WLAN", "wifi"),
        ("Wi-Fi", "wifi"),
        ("无线网络连接", "wifi"),
        ("以太网", "ethernet"),
        ("Ethernet", "ethernet"),
        ("Realtek PCIe GbE Family Controller", "ethernet"),
        ("本地连接* 1", "other"),      # Wi-Fi Direct 虚拟网卡，不能算有线
        ("未知网卡", "other"),
    ])
    def test_guess_interface_type(self, name, expected):
        assert network._guess_interface_type(name) == expected

    @pytest.mark.parametrize("name", [
        "VMware Network Adapter VMnet1",
        "vEthernet (Default Switch)",
        "Hyper-V Virtual Ethernet Adapter",
        "VirtualBox Host-Only Network",
        "本地连接* 2",
        "TAP-Windows Adapter V9",
        "Tailscale",
    ])
    def test_virtual_is_detected(self, name):
        assert network._is_virtual_interface(name) is True

    @pytest.mark.parametrize("name", ["WLAN", "Wi-Fi", "以太网", "Ethernet", "本地连接"])
    def test_physical_is_not_virtual(self, name):
        assert network._is_virtual_interface(name) is False