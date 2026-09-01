"""
Dashboard 模式切换 UI 单元测试。
"""

from unittest.mock import patch, MagicMock

import pytest

from phonemic.gui.dashboard import Dashboard
from phonemic.tunnel.mode import TunnelMode


@pytest.fixture
def dashboard(qtbot):
    with patch("phonemic.gui.dashboard.get_mode", return_value=TunnelMode.LAN):
        d = Dashboard("192.168.1.100", 12000)
        d._is_force_quitting = True  # prevent closeEvent tray access
        qtbot.addWidget(d)
        yield d


class TestModeToggle:
    def test_default_mode_is_lan(self, dashboard):
        assert dashboard.get_mode() == TunnelMode.LAN

    def test_lan_action_checked_by_default(self, dashboard):
        assert dashboard.act_lan.isChecked() is True

    def test_cf_action_not_checked_by_default(self, dashboard):
        assert dashboard.act_cf.isChecked() is False

    def test_info_label_visible_in_lan_mode(self, dashboard):
        assert not dashboard.info_label.isHidden()

    def test_click_cf_switches_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        assert dashboard.get_mode() == TunnelMode.CLOUDFLARE

    def test_cf_action_checked_after_switch(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_cf.isChecked() is True

    def test_lan_action_not_checked_after_switch(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_lan.isChecked() is False

    def test_info_label_hidden_in_cf_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.info_label.isHidden()

    def test_mode_switch_callback_called(self, dashboard):
        cb = MagicMock()
        dashboard.set_mode_switch_callback(cb)
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        cb.assert_called_once_with(TunnelMode.CLOUDFLARE)

    def test_click_same_mode_no_callback(self, dashboard):
        cb = MagicMock()
        dashboard.set_mode_switch_callback(cb)
        dashboard._on_mode_clicked(TunnelMode.LAN)
        cb.assert_not_called()

    def test_click_lan_back_from_cf(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._on_mode_clicked(TunnelMode.LAN)
        dashboard.on_switch_completed()
        assert dashboard.get_mode() == TunnelMode.LAN
        assert not dashboard.info_label.isHidden()


class TestSwitchNetworkAction:
    def test_switch_network_enabled_in_lan(self, dashboard):
        assert dashboard.switch_network_action.isEnabled() is True

    def test_switch_network_disabled_in_cf_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.switch_network_action.isEnabled() is False

    def test_switch_network_re_enabled_back_to_lan(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._on_mode_clicked(TunnelMode.LAN)
        dashboard.on_switch_completed()
        assert dashboard.switch_network_action.isEnabled() is True


class TestTunnelUrlUpdate:
    def test_update_tunnel_url_updates_qr(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" in dashboard.url_text.toPlainText()

    def test_update_tunnel_url_none_shows_disconnected(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url(None)
        assert dashboard._tunnel_url is None

    def test_update_tunnel_url_not_applied_in_lan_mode(self, dashboard):
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" not in dashboard.url_text.toPlainText()


class TestUrlDisplay:
    """地址栏用只读 QTextEdit，便于横向滚动/选中/复制。"""

    def test_url_text_widget_exists(self, dashboard):
        from PySide6.QtWidgets import QTextEdit
        assert isinstance(dashboard.url_text, QTextEdit)
        assert dashboard.url_text.isReadOnly() is True

    def test_url_text_shows_lan_url(self, dashboard):
        assert "192.168.1.100:12000" in dashboard.url_text.toPlainText()

    def test_url_text_updates_after_tunnel_ready(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://example.trycloudflare.com")
        assert "example.trycloudflare.com" in dashboard.url_text.toPlainText()


class TestEncryptionModeRestriction:
    """Cloudflare 模式必须加密：none 选项禁用，配置为 none 时实际使用 xchacha20。"""

    def test_algo_none_enabled_in_lan_by_default(self, dashboard):
        assert dashboard.act_algo_none.isEnabled() is True

    def test_algo_none_disabled_in_cf_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_algo_none.isEnabled() is False

    def test_algo_none_re_enabled_back_to_lan(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._on_mode_clicked(TunnelMode.LAN)
        dashboard.on_switch_completed()
        assert dashboard.act_algo_none.isEnabled() is True

    def test_cf_with_config_none_shows_xchacha20_checked(self, dashboard):
        """配置为 none 时进入 CF 模式：none 禁用 + xchacha20 勾选。"""
        # 模拟配置为 none 的场景
        dashboard._algorithm = "none"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_algo_none.isEnabled() is False
        assert dashboard.act_algo_xchacha20.isChecked() is True
        assert dashboard.act_algo_none.isChecked() is False

    def test_clicking_none_in_cf_mode_does_not_change_algorithm(self, dashboard):
        """CF 模式下点 none 应被拒绝，配置不变。"""
        dashboard._algorithm = "xsalsa20"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._sync_menu_checks()
        dashboard._on_algorithm_clicked("none")
        # 应保持 xsalsa20，未切到 none
        assert dashboard._algorithm == "xsalsa20"
        assert dashboard.act_algo_xsalsa20.isChecked() is True
        assert dashboard.act_algo_none.isChecked() is False
