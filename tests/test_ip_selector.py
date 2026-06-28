"""
测试 ip_selector 模块的 select_lan_ip 函数
覆盖三种选择模式（由外部传入，但本模块只负责 ask 模式）
及边界情况
"""

import pytest
from unittest.mock import patch, MagicMock
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog
from phonemic.gui.ip_selector import select_lan_ip
from phonemic.utils.network import IpCandidate

# ----------------------- Fixtures -----------------------
@pytest.fixture
def mock_candidates():
    """返回一组模拟的候选 IP（含 MAC）"""
    return [
        IpCandidate(
            ip="192.168.1.100",
            interface_name="Ethernet0",
            description="Realtek",
            priority=0,
            is_virtual=False,
            is_default_gateway_match=True,
            interface_type="ethernet",
            metric=0,
            mac="AA:BB:CC:DD:EE:01"
        ),
        IpCandidate(
            ip="10.0.0.2",
            interface_name="Wi-Fi",
            description="Intel Wi-Fi",
            priority=5,
            is_virtual=False,
            is_default_gateway_match=False,
            interface_type="wifi",
            metric=0,
            mac="AA:BB:CC:DD:EE:02"
        ),
        IpCandidate(
            ip="172.16.0.3",
            interface_name="VMware",
            description="VMware Virtual",
            priority=10,
            is_virtual=True,
            is_default_gateway_match=False,
            interface_type="virtual",
            metric=0,
            mac="11:22:33:44:55:66"
        ),
    ]


# ----------------------- 测试 select_lan_ip (ask 模式) -----------------------
def test_select_lan_ip_ask_single_candidate(qtbot, mock_candidates):
    """当只有一个候选时，应直接返回该候选的 (ip, mac)，不弹窗"""
    with patch('phonemic.gui.ip_selector.get_all_lan_ips', return_value=[mock_candidates[0]]):
        ip, mac = select_lan_ip()
        assert ip == "192.168.1.100"
        assert mac == "AA:BB:CC:DD:EE:01"


def test_select_lan_ip_ask_multiple_candidates(qtbot, mock_candidates):
    """多个候选时弹出对话框，模拟用户选择第一个"""
    with patch('phonemic.gui.ip_selector.get_all_lan_ips', return_value=mock_candidates):
        # Mock IpSelector 的 exec 返回 Accepted，并让 get_selected_candidate 返回第一个候选
        with patch('phonemic.gui.ip_selector.IpSelector.exec', return_value=QDialog.Accepted):
            with patch('phonemic.gui.ip_selector.IpSelector.get_selected_candidate', return_value=mock_candidates[0]):
                ip, mac = select_lan_ip()
                assert ip == "192.168.1.100"
                assert mac == "AA:BB:CC:DD:EE:01"


def test_select_lan_ip_ask_cancel(qtbot, mock_candidates):
    """用户取消选择，应返回 (None, None)"""
    with patch('phonemic.gui.ip_selector.get_all_lan_ips', return_value=mock_candidates):
        with patch('phonemic.gui.ip_selector.IpSelector.exec', return_value=QDialog.Rejected):
            ip, mac = select_lan_ip()
            assert ip is None
            assert mac is None


def test_select_lan_ip_no_candidates(qtbot):
    """没有可用 IP 时，返回 (None, None) 并弹出错误提示（此处我们 mock QMessageBox）"""
    with patch('phonemic.gui.ip_selector.get_all_lan_ips', return_value=[]):
        # 同时 mock QMessageBox.critical 以避免实际弹窗
        with patch('phonemic.gui.ip_selector.QMessageBox.critical') as mock_critical:
            ip, mac = select_lan_ip()
            assert ip is None
            assert mac is None
            mock_critical.assert_called_once()


# ----------------------- 测试 IpSelector 自身的功能（可选）------------------
def test_ip_selector_initial_selection(qtbot):
    """测试 IpSelector 默认选中第一项"""
    from phonemic.gui.ip_selector import IpSelector
    candidates = [
        IpCandidate("192.168.1.1", "eth0", "Ethernet", 0, mac="00:11:22:33:44:55"),
        IpCandidate("10.0.0.1", "wlan0", "Wi-Fi", 1, mac="AA:BB:CC:DD:EE:FF"),
    ]
    selector = IpSelector(candidates)
    qtbot.addWidget(selector)
    # 默认应选中第一项
    assert selector.list_widget.currentRow() == 0
    # 验证该项存储的数据是索引 0
    item = selector.list_widget.currentItem()
    assert item.data(Qt.UserRole) == 0
    selector.close()


def test_ip_selector_get_selected_candidate_after_accept(qtbot):
    """模拟用户点击确定，验证 get_selected_candidate 返回正确候选"""
    from phonemic.gui.ip_selector import IpSelector
    candidates = [
        IpCandidate("192.168.1.1", "eth0", "Ethernet", 0, mac="00:11:22:33:44:55"),
        IpCandidate("10.0.0.1", "wlan0", "Wi-Fi", 1, mac="AA:BB:CC:DD:EE:FF"),
    ]
    selector = IpSelector(candidates)
    qtbot.addWidget(selector)
    # 选择第二项
    selector.list_widget.setCurrentRow(1)
    # 模拟确定
    selector.accept()  # 触发 accept
    sel = selector.get_selected_candidate()
    assert sel is candidates[1]
    selector.close()