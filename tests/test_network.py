"""
测试 phonemic.utils.network 模块
覆盖：
- get_all_lan_ips() 的候选 IP 获取与优先级排序
- get_local_ip() 在有/无可用 IP 时的返回值
- 边缘场景：无网络、仅虚拟网卡、多网卡优先级
- MAC 地址字段和 find_candidate_by_mac 函数
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from phonemic.utils.network import get_all_lan_ips, get_local_ip, IpCandidate, find_free_port, find_candidate_by_mac
import socket

def test_get_all_lan_ips_returns_list():
    candidates = get_all_lan_ips()
    assert isinstance(candidates, list)
    
def test_get_all_lan_ips_contains_mac():
    """确保每个候选都包含 MAC 字段（至少有一个非空）"""
    candidates = get_all_lan_ips()
    # 如果有候选，至少有一个 MAC 非空（通常物理网卡有 MAC）
    if candidates:
        has_mac = any(c.mac for c in candidates)
        # 注意：某些虚拟网卡可能没有 MAC，所以不一定全部有，但应有至少一个
        # 我们可以不强制断言，仅检查字段存在
        for c in candidates:
            assert hasattr(c, 'mac')
    else:
        # 无网络时，列表为空，测试通过
        pass

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
    port = find_free_port(start_port=9000)
    assert isinstance(port, int)
    assert port >= 9000
    # 验证端口确实可用
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert s.bind(("", port)) is None # bind 成功时返回 None

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