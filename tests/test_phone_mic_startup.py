"""
PhoneMic 启动流程单元测试
覆盖：命令行参数解析、网络选择策略、静默启动、服务器启动、错误处理。
"""

import sys
import pytest
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

import phonemic.PhoneMic as pm
from phonemic.gui import direct_input
from phonemic.gui.direct_input import ABANDONED, IDLE, TYPING, DirectInputController
from phonemic.utils.i18n import I18n
from phonemic.utils.network import IpCandidate


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_components(mocker):
    """Mock 所有外部依赖，返回常用 mock 对象"""
    # 网络和端口
    mock_get_ips = mocker.patch.object(pm, "get_all_lan_ips")
    mock_find_port = mocker.patch.object(pm, "find_free_port", return_value=8008)
    mock_start_server = mocker.patch.object(pm, "start_server")
    mock_wait = mocker.patch.object(pm, "wait_for_server", return_value=True)

    # GUI 组件
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mock_hud = mocker.patch.object(pm, "HudWindow")
    mock_tray = mocker.patch.object(pm, "SystemTray")
    mock_command = mocker.patch.object(pm, "CommandInterceptor")

    # 线程和队列
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")

    # SettingsManager
    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "auto",
        "last_network_mac": None
    }.get(key, default)

    # ====== 关键：mock QApplication.exec 防止卡住 ======
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    # 返回所有 mock 对象以便测试断言
    return {
        "get_ips": mock_get_ips,
        "find_port": mock_find_port,
        "start_server": mock_start_server,
        "wait": mock_wait,
        "dashboard": mock_dashboard,
        "hud": mock_hud,
        "tray": mock_tray,
        "command": mock_command,
        "sm": mock_sm_instance,
    }


# ---------- 命令行参数解析测试 ----------
def test_parse_args_default():
    sys.argv = ["PhoneMic.py"]
    args = pm.parse_args()
    assert args.silent is False
    assert args.select_mode is None


def test_parse_args_silent():
    sys.argv = ["PhoneMic.py", "--silent"]
    args = pm.parse_args()
    assert args.silent is True


def test_parse_args_select_mode():
    sys.argv = ["PhoneMic.py", "--select-mode", "last"]
    args = pm.parse_args()
    assert args.select_mode == "last"


# ---------- 主流程测试 ----------
def test_main_auto_mode(qapp, mocker, mock_components):
    """
    测试 auto 模式：选择第一个候选，启动服务器，显示窗口。
    """
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
    ]
    mock_components["get_ips"].return_value = candidates

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_components["dashboard"].assert_called_once_with("192.168.1.100", 8008)
    mock_components["dashboard"].return_value.show.assert_called_once()
    # 验证保存 MAC（新逻辑）
    mock_components["sm"].set.assert_called_once_with("last_network_mac", "AA:BB:CC:DD:EE:01")


def test_main_silent_mode(qapp, mocker, mock_components):
    """测试 --silent 参数：不显示 Dashboard"""
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
    ]
    mock_components["get_ips"].return_value = candidates

    sys.argv = ["PhoneMic.py", "--silent"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_components["dashboard"].assert_called_once()
    mock_components["dashboard"].return_value.show.assert_not_called()
    mock_components["dashboard"].return_value.hide.assert_called_once()


def test_main_last_mode_match(qapp, mocker):
    """
    测试 last 模式：上次 MAC 匹配成功，使用该 IP。
    """
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01"),
        IpCandidate("10.0.0.2", "wlan0", "Wi-Fi", 1, mac="BB:CC:DD:EE:FF:02"),
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=True)
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mocker.patch.object(pm, "HudWindow")
    mocker.patch.object(pm, "SystemTray")
    mocker.patch.object(pm, "CommandInterceptor")
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "last",
        "last_network_mac": "BB:CC:DD:EE:FF:02"
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_dashboard.assert_called_once_with("10.0.0.2", 8008)
    # 应保存 MAC（与上次相同，但依然保存）
    mock_sm_instance.set.assert_called_once_with("last_network_mac", "BB:CC:DD:EE:FF:02")


def test_main_last_mode_no_match(qapp, mocker):
    """
    测试 last 模式：MAC 不匹配，回退到 auto（第一个候选），并保存该 MAC。
    """
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01"),
        IpCandidate("10.0.0.2", "wlan0", "Wi-Fi", 1, mac="BB:CC:DD:EE:FF:02"),
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=True)
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mocker.patch.object(pm, "HudWindow")
    mocker.patch.object(pm, "SystemTray")
    mocker.patch.object(pm, "CommandInterceptor")
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "last",
        "last_network_mac": "XX:XX:XX:XX:XX:XX"
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_dashboard.assert_called_once_with("192.168.1.100", 8008)
    # 回退后保存新的 MAC
    mock_sm_instance.set.assert_called_once_with("last_network_mac", "AA:BB:CC:DD:EE:01")


def test_main_ask_mode_confirm(qapp, mocker):
    """
    测试 ask 模式：用户选择了一个 IP，保存 MAC。
    """
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01"),
        IpCandidate("10.0.0.2", "wlan0", "Wi-Fi", 1, mac="BB:CC:DD:EE:FF:02"),
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=True)
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mocker.patch.object(pm, "HudWindow")
    mocker.patch.object(pm, "SystemTray")
    mocker.patch.object(pm, "CommandInterceptor")
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    mocker.patch.object(pm, "select_lan_ip", return_value=("10.0.0.2", "BB:CC:DD:EE:FF:02"))

    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "ask",
        "last_network_mac": None
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_dashboard.assert_called_once_with("10.0.0.2", 8008)
    mock_sm_instance.set.assert_called_once_with("last_network_mac", "BB:CC:DD:EE:FF:02")


def test_main_ask_mode_cancel(qapp, mocker):
    """
    测试 ask 模式：用户取消，程序退出（不保存 MAC）。
    """
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01"),
        IpCandidate("10.0.0.2", "wlan0", "Wi-Fi", 1, mac="BB:CC:DD:EE:FF:02"),
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=True)
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mocker.patch.object(pm, "HudWindow")
    mocker.patch.object(pm, "SystemTray")
    mocker.patch.object(pm, "CommandInterceptor")
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    mocker.patch.object(pm, "select_lan_ip", return_value=(None, None))

    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "ask",
        "last_network_mac": None
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_dashboard.assert_not_called()
    mock_sm_instance.set.assert_not_called()  # 取消时不保存


def test_main_last_mode_single_network_saves_mac(qapp, mocker):
    """只有一个网络时，last 模式应保存 MAC"""
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=True)
    mock_dashboard = mocker.patch.object(pm, "Dashboard")
    mocker.patch.object(pm, "HudWindow")
    mocker.patch.object(pm, "SystemTray")
    mocker.patch.object(pm, "CommandInterceptor")
    mocker.patch("threading.Thread")
    mocker.patch("multiprocessing.Queue")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm_instance = mock_sm.instance.return_value
    mock_sm_instance.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "last",
        "last_network_mac": None
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 0

    mock_sm_instance.set.assert_called_once_with("last_network_mac", "AA:BB:CC:DD:EE:01")


def test_main_no_candidates(qapp, mocker):
    """没有可用 IP，程序退出并报错"""
    mocker.patch.object(pm, "get_all_lan_ips", return_value=[])
    mock_QMessageBox = mocker.patch("phonemic.PhoneMic.QMessageBox")
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 1

    mock_QMessageBox.critical.assert_called_once()
    
    i18n = I18n.instance()
    expected = i18n.tr("error.no_lan_ip")
    mock_QMessageBox.critical.assert_called_once()
    assert expected in mock_QMessageBox.critical.call_args[0][2]


def test_main_no_free_port(qapp, mocker):
    """找不到可用端口，程序退出并报错"""
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=None)
    mock_QMessageBox = mocker.patch("phonemic.PhoneMic.QMessageBox")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    # 必须 mock SettingsManager：否则会读取真实配置，
    # network_selection_mode 默认为 "ask"，将弹出真实网络选择对话框
    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm.instance.return_value.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "auto",
        "last_network_mac": None,
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 1

    mock_QMessageBox.critical.assert_called_once()
    i18n = I18n.instance()
    expected = i18n.tr("error.no_free_port")
    mock_QMessageBox.critical.assert_called_once()
    assert expected in mock_QMessageBox.critical.call_args[0][2]


def test_main_server_start_timeout(qapp, mocker):
    """服务器启动超时，退出并报错"""
    candidates = [
        IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
    ]
    mocker.patch.object(pm, "get_all_lan_ips", return_value=candidates)
    mocker.patch.object(pm, "find_free_port", return_value=8008)
    mocker.patch.object(pm, "start_server")
    mocker.patch.object(pm, "wait_for_server", return_value=False)
    mock_QMessageBox = mocker.patch("phonemic.PhoneMic.QMessageBox")
    mocker.patch.object(pm.QApplication, 'exec', return_value=0)

    # 必须 mock SettingsManager：否则会读取真实配置，
    # network_selection_mode 默认为 "ask"，将弹出真实网络选择对话框
    mock_sm = mocker.patch.object(pm, "SettingsManager")
    mock_sm.instance.return_value.get.side_effect = lambda key, default=None: {
        "network_selection_mode": "auto",
        "last_network_mac": None,
    }.get(key, default)

    sys.argv = ["PhoneMic.py"]

    with pytest.raises(SystemExit) as exc:
        pm.main()
    assert exc.value.code == 1

    mock_QMessageBox.critical.assert_called_once()
    i18n = I18n.instance()
    expected = i18n.tr("error.server_timeout")
    mock_QMessageBox.critical.assert_called_once()
    assert expected[:7] in mock_QMessageBox.critical.call_args[0][2]


# ---------- 直接输入会话的边界清理 ----------
#
# 回归：direct_input 是模块级单例。会话停在 ABANDONED 会「粘」住下一轮——
# 新的 preview 调到 update() 一直返回 False（不再打字），整轮静默退化成
# 「预览 + 悬浮窗」。用户看到的现象是「direct 模式忽然不实时上屏了」。
#
# 缺的测试环在 PhoneMic.main()：test_phone_mic_startup 一直跑完整 main()，
# 但从不派发任何后端事件，disconnect 分支与配置订阅全在视野之外。

class _Focus:
    """可在测试中改写的焦点指纹探针。"""

    def __init__(self, sig=(1, 1)):
        self.sig = sig

    def __call__(self):
        return self.sig


class _BridgeSpy:
    """QtEventBridge 替身：记录被连接的槽，便于测试直接派发后端事件。"""

    class _Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

    def __init__(self):
        self.event_signal = self._Signal()

    def emit(self, event_type, payload=None):
        for slot in list(self.event_signal.slots):
            slot(event_type, payload)


def _mode_handler(sm):
    """取出 main() 注册在 "text_input_mode" 上的回调。"""
    for call in sm.connect_changed.call_args_list:
        if call.args and call.args[0] == "text_input_mode":
            return call.args[1]
    raise AssertionError("main() 未订阅 text_input_mode 变更")


class TestDirectSessionBoundaries:
    @staticmethod
    def _run_main(mocker, mock_components):
        """跑一遍 main()，返回 (bridge 替身, direct 控制器, 焦点探针)。"""
        focus = _Focus()
        ctrl = DirectInputController(focus)
        mocker.patch.object(pm.direct_input, "_controller", ctrl)
        # 不真敲键盘
        mocker.patch.object(pm.direct_input.text_input, "send_text", lambda t: None)
        mocker.patch.object(pm.direct_input.text_input, "send_backspace", lambda n: None)

        spy = _BridgeSpy()
        mocker.patch.object(pm, "QtEventBridge", return_value=spy)

        mock_components["get_ips"].return_value = [
            IpCandidate("192.168.1.100", "eth0", "Ethernet", 0, mac="AA:BB:CC:DD:EE:01")
        ]
        sys.argv = ["PhoneMic.py"]
        with pytest.raises(SystemExit) as exc:
            pm.main()
        assert exc.value.code == 0
        assert len(spy.event_signal.slots) == 1, "main() 应只注册一个后端事件处理函数"
        return spy, ctrl, focus

    @staticmethod
    def _abandon(ctrl, focus):
        """把会话推进到 ABANDONED：打一次字，再让焦点指纹变化。"""
        ctrl.update("你好")
        assert ctrl.state == TYPING
        focus.sig = (2, 2)                     # 用户切了窗口
        assert ctrl.update("你好世界") is False
        assert ctrl.state == ABANDONED

    def test_disconnect_clears_abandoned_session(self, qapp, mocker, mock_components):
        spy, ctrl, focus = self._run_main(mocker, mock_components)
        self._abandon(ctrl, focus)

        spy.event_signal.slots[0]("disconnect", None)

        assert ctrl.state == IDLE
        # 下一轮能正常重新开始（修复前 update() 会一直返回 False）
        assert ctrl.update("新的句子") is True

    def test_switch_to_direct_clears_abandoned_session(self, qapp, mocker, mock_components):
        spy, ctrl, focus = self._run_main(mocker, mock_components)
        self._abandon(ctrl, focus)

        _mode_handler(mock_components["sm"])("direct")

        assert ctrl.state == IDLE

    def test_switch_away_from_direct_keeps_session(self, qapp, mocker, mock_components):
        """切离 direct 刻意不重置：输入框里可能还留着 preview 打出的文字，
        一旦重置，随后的 discard_preview() 会变成空操作，命令命中时就少删了
        那串字面文字（见 PhoneMic.main 里的注释）。"""
        spy, ctrl, focus = self._run_main(mocker, mock_components)
        ctrl.update("你好")
        assert ctrl.state == TYPING

        _mode_handler(mock_components["sm"])("paste")

        assert ctrl.state == TYPING
        assert ctrl.typed == "你好"