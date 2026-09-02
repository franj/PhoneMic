"""
Dashboard 模式切换 UI 单元测试。
"""

from unittest.mock import patch, MagicMock

import pytest

from phonemic.gui.dashboard import Dashboard
from phonemic.tunnel.mode import TunnelMode
from phonemic.utils.settings_manager import SettingsManager


@pytest.fixture
def dashboard(qtbot, tmp_path, monkeypatch):
    # 隔离配置：指向临时目录并重置单例，避免读写真实用户配置
    import phonemic.utils.settings_manager as sm_mod
    monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
    SettingsManager._instance = None
    try:
        with patch("phonemic.gui.dashboard.get_mode", return_value=TunnelMode.LAN):
            d = Dashboard("192.168.1.100", 12000)
            d._is_force_quitting = True  # prevent closeEvent tray access
            qtbot.addWidget(d)
            yield d
    finally:
        SettingsManager._instance = None


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
        assert "test.trycloudflare.com" in dashboard.ip_label.text()

    def test_update_tunnel_url_none_shows_disconnected(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url(None)
        assert dashboard._tunnel_url is None

    def test_update_tunnel_url_not_applied_in_lan_mode(self, dashboard):
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" not in dashboard.ip_label.text()


class TestUrlDisplay:
    """地址栏用 QLabel：居中、自动换行、可用鼠标选中复制。"""

    def test_url_widget_is_label(self, dashboard):
        from PySide6.QtWidgets import QLabel
        assert isinstance(dashboard.ip_label, QLabel)

    def test_url_label_shows_lan_url(self, dashboard):
        assert "192.168.1.100:12000" in dashboard.ip_label.text()

    def test_url_label_updates_after_tunnel_ready(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://example.trycloudflare.com")
        assert "example.trycloudflare.com" in dashboard.ip_label.text()

    def test_url_label_is_centered(self, dashboard):
        """QLabel 的对齐是控件级属性，setText 后依然保持居中。"""
        from PySide6.QtCore import Qt
        assert dashboard.ip_label.alignment() == Qt.AlignCenter

    def test_url_label_keeps_centering_after_updates(self, dashboard):
        """多次更新 URL 后仍居中（不像 QTextEdit 那样会被重置）。"""
        from PySide6.QtCore import Qt
        dashboard.ip_label.setText("https://a.trycloudflare.com")
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.ip_label.setText("https://b.trycloudflare.com")
        assert dashboard.ip_label.alignment() == Qt.AlignCenter

    def test_url_label_supports_long_wrapping(self, dashboard):
        """长 URL 自动换行，文本完整保留。"""
        long_url = "https://very-long-subdomain-name-that-will-wrap.trycloudflare.com:8443"
        dashboard.ip_label.setText(long_url)
        assert dashboard.ip_label.wordWrap() is True
        assert dashboard.ip_label.text() == long_url

    def test_url_label_is_selectable(self, dashboard):
        """可用鼠标选中地址文本以便复制。"""
        from PySide6.QtCore import Qt
        flags = dashboard.ip_label.textInteractionFlags()
        assert (flags & Qt.TextSelectableByMouse) == Qt.TextSelectableByMouse


class TestEncryptionModeRestriction:
    """Cloudflare 模式必须加密：none 选项禁用，配置为 none 时实际强制加密。"""

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

    def test_cf_with_config_none_shows_encrypted_checked(self, dashboard):
        """配置为 none 时进入 CF 模式：none 禁用 + 加密勾选。"""
        # 模拟配置为 none 的场景
        dashboard._algorithm = "none"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_algo_none.isEnabled() is False
        assert dashboard.act_algo_encrypted.isChecked() is True
        assert dashboard.act_algo_none.isChecked() is False

    def test_clicking_none_in_cf_mode_does_not_change_algorithm(self, dashboard):
        """CF 模式下点 none 应被拒绝，配置不变。"""
        dashboard._algorithm = "auto"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._sync_menu_checks()
        dashboard._on_algorithm_clicked("none")
        # 应保持 auto（加密），未切到 none
        assert dashboard._algorithm == "auto"
        assert dashboard.act_algo_encrypted.isChecked() is True
        assert dashboard.act_algo_none.isChecked() is False


class TestEncryptionToggle:
    """加密菜单只有 加密/不加密 两项，具体算法由客户端协商。"""

    def test_menu_has_no_algorithm_items(self, dashboard):
        """不再暴露具体算法菜单项。"""
        assert not hasattr(dashboard, "act_algo_xsalsa20")
        assert not hasattr(dashboard, "act_algo_xchacha20")

    def test_default_is_none(self, dashboard):
        """默认不加密（LAN 模式）。"""
        assert dashboard.act_algo_none.isChecked() is True
        assert dashboard.act_algo_encrypted.isChecked() is False

    def test_clicking_encrypted_persists_auto(self, dashboard):
        """点击加密：配置写入 auto，加密项勾选。"""
        dashboard._on_algorithm_clicked("auto")
        assert dashboard._algorithm == "auto"
        assert dashboard.sm.get("e2ee_algorithm") == "auto"
        assert dashboard.act_algo_encrypted.isChecked() is True
        assert dashboard.act_algo_none.isChecked() is False

    def test_clicking_none_back_in_lan(self, dashboard):
        """LAN 模式下可从加密切回不加密。"""
        dashboard._on_algorithm_clicked("auto")
        dashboard._on_algorithm_clicked("none")
        assert dashboard._algorithm == "none"
        assert dashboard.sm.get("e2ee_algorithm") == "none"
        assert dashboard.act_algo_none.isChecked() is True

    def test_status_shows_negotiated_algorithm(self, dashboard):
        """加密连接时状态栏显示协商出的具体算法（信息透明，菜单不让用户选）。"""
        dashboard._algorithm = "auto"
        dashboard.update_connection_status(True, "xchacha20")
        assert "XChaCha20" in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_encrypted_algo", algo="XChaCha20") \
            in dashboard.status_label.text()

    def test_status_encrypted_without_negotiated_algo(self, dashboard):
        """未携带协商算法（如模式切换后刷新）时退化为通用 加密 文案。"""
        dashboard._algorithm = "auto"
        dashboard.update_connection_status(True)
        assert "XChaCha20" not in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_encrypted") in dashboard.status_label.text()

    def test_status_negotiated_algo_cleared_on_disconnect(self, dashboard):
        """断开后清空协商结果，明文模式不显示算法。"""
        dashboard._algorithm = "auto"
        dashboard.update_connection_status(True, "xchacha20")
        assert dashboard._negotiated_algo == "xchacha20"
        dashboard.update_connection_status(False)
        assert dashboard._negotiated_algo is None
        dashboard._algorithm = "none"
        dashboard.update_connection_status(True)
        assert "XChaCha20" not in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_plaintext") in dashboard.status_label.text()

    def test_algo_display_name_fallback_for_unknown_algo(self, dashboard):
        """locale 缺失的算法名回退为原始算法名（未来新增算法无需先加 locale）。"""
        assert dashboard._algo_display_name("xchacha20") == "XChaCha20"
        assert dashboard._algo_display_name("aes-256-gcm") == "aes-256-gcm"
