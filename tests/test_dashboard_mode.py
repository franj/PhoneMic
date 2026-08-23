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

    def test_lan_button_active_style(self, dashboard):
        style = dashboard.btn_lan.styleSheet()
        assert "#07c160" in style

    def test_cf_button_inactive_style(self, dashboard):
        style = dashboard.btn_cf.styleSheet()
        assert "#07c160" not in style

    def test_info_label_visible_in_lan_mode(self, dashboard):
        assert not dashboard.info_label.isHidden()

    def test_click_cf_switches_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        assert dashboard.get_mode() == TunnelMode.CLOUDFLARE

    def test_cf_button_active_after_switch(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        style = dashboard.btn_cf.styleSheet()
        assert "#07c160" in style

    def test_lan_button_inactive_after_switch(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        style = dashboard.btn_lan.styleSheet()
        assert "#07c160" not in style

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


class TestTunnelUrlUpdate:
    def test_update_tunnel_url_updates_qr(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" in dashboard.ip_label.text()

    def test_update_tunnel_url_none_shows_disconnected(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url(None)
        assert dashboard._tunnel_url is None

    def test_update_tunnel_url_not_applied_in_lan_mode(self, dashboard):
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" not in dashboard.ip_label.text()
