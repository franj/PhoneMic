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


class TestInputModeMenu:
    """「上屏方式」菜单：剪贴板 / 模拟键盘 / 直接输入 三选一，与偏好设置面板等价。"""

    def test_default_is_paste(self, dashboard):
        assert dashboard.act_input_paste.isChecked() is True
        assert dashboard.act_input_type.isChecked() is False
        assert dashboard.act_input_direct.isChecked() is False

    def test_menu_title_present(self, dashboard):
        titles = [a.text() for a in dashboard.menuBar().actions()]
        assert dashboard.i18n.tr("dashboard.menu_input_mode") in titles

    def test_menu_order_right_after_network(self, dashboard):
        titles = [a.text() for a in dashboard.menuBar().actions()]
        assert titles.index(dashboard.i18n.tr("dashboard.menu_input_mode")) == \
            titles.index(dashboard.i18n.tr("dashboard.menu_network")) + 1

    def test_clicking_type_persists_config(self, dashboard):
        dashboard._on_input_mode_clicked("type")
        assert dashboard.sm.get("text_input_mode") == "type"
        assert dashboard.act_input_type.isChecked() is True
        assert dashboard.act_input_paste.isChecked() is False

    def test_clicking_paste_back(self, dashboard):
        dashboard._on_input_mode_clicked("type")
        dashboard._on_input_mode_clicked("paste")
        assert dashboard.sm.get("text_input_mode") == "paste"
        assert dashboard.act_input_paste.isChecked() is True
        assert dashboard.act_input_type.isChecked() is False

    def test_triggering_action_switches(self, dashboard):
        """直接触发菜单项（等价于用户点击）也能切到模拟键盘。"""
        dashboard.act_input_type.trigger()
        assert dashboard.sm.get("text_input_mode") == "type"
        assert dashboard.act_input_type.isChecked() is True

    def test_external_change_syncs_checks(self, dashboard):
        """偏好设置 / 托盘菜单改动配置后，主界面菜单勾选应同步。"""
        dashboard.sm.set("text_input_mode", "type")
        assert dashboard.act_input_type.isChecked() is True
        assert dashboard.act_input_paste.isChecked() is False

    def test_uses_exclusive_group_like_network_menu(self, dashboard):
        """与「网络」菜单外观统一：用互斥组（Qt 画成单选圆点）。"""
        group = dashboard.act_input_paste.actionGroup()
        assert group is not None
        assert group.isExclusive() is True
        assert dashboard.act_input_type.actionGroup() is group
        assert len(group.actions()) == 3

    def test_reclicking_selected_item_keeps_checked(self, dashboard):
        """互斥组：重复点击已选中项不会被取消勾选。"""
        dashboard.act_input_paste.trigger()
        assert dashboard.act_input_paste.isChecked() is True
        assert dashboard.act_input_type.isChecked() is False


class TestDirectInputMenu:
    """第三项「模拟键盘（直接输入）」与其它两项互斥且同样即时生效。"""

    def test_clicking_direct_persists_config(self, dashboard):
        dashboard._on_input_mode_clicked("direct")
        assert dashboard.sm.get("text_input_mode") == "direct"
        assert dashboard.act_input_direct.isChecked() is True
        assert dashboard.act_input_paste.isChecked() is False
        assert dashboard.act_input_type.isChecked() is False

    def test_triggering_action_switches(self, dashboard):
        dashboard.act_input_direct.trigger()
        assert dashboard.sm.get("text_input_mode") == "direct"
        assert dashboard.act_input_direct.isChecked() is True

    def test_switching_back_to_paste(self, dashboard):
        dashboard._on_input_mode_clicked("direct")
        dashboard._on_input_mode_clicked("paste")
        assert dashboard.sm.get("text_input_mode") == "paste"
        assert dashboard.act_input_paste.isChecked() is True
        assert dashboard.act_input_direct.isChecked() is False

    def test_external_change_syncs_checks(self, dashboard):
        """偏好设置 / 托盘菜单改动配置后，主界面菜单勾选应同步。"""
        dashboard.sm.set("text_input_mode", "direct")
        assert dashboard.act_input_direct.isChecked() is True
        assert dashboard.act_input_type.isChecked() is False
        assert dashboard.act_input_paste.isChecked() is False

    def test_in_same_exclusive_group(self, dashboard):
        assert dashboard.act_input_direct.actionGroup() is \
            dashboard.act_input_paste.actionGroup()


class TestTunnelUrlUpdate:
    def test_update_tunnel_url_updates_qr(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" in dashboard.ip_label.toPlainText()

    def test_update_tunnel_url_none_shows_disconnected(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url(None)
        assert dashboard._tunnel_url is None

    def test_update_tunnel_url_not_applied_in_lan_mode(self, dashboard):
        dashboard.update_tunnel_url("https://test.trycloudflare.com")
        assert "test.trycloudflare.com" not in dashboard.ip_label.toPlainText()


class TestUrlDisplay:
    """地址栏用 QLabel/QLineEdit(都可以，显示出文本就行)：居中、可用鼠标选中复制。"""

    def test_url_label_shows_lan_url(self, dashboard):
        assert "192.168.1.100:12000" in dashboard.ip_label.toPlainText()

    def test_url_label_updates_after_tunnel_ready(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.update_tunnel_url("https://example.trycloudflare.com")
        assert "example.trycloudflare.com" in dashboard.ip_label.toPlainText()

    def test_url_label_is_centered(self, dashboard):
        """对齐是控件级属性，setText 后依然保持居中。"""
        from PySide6.QtCore import Qt
        assert dashboard.ip_label.alignment() == Qt.AlignCenter

    def test_url_label_keeps_centering_after_updates(self, dashboard):
        """多次更新 URL 后仍居中（不像 QTextEdit 那样会被重置）。"""
        from PySide6.QtCore import Qt
        dashboard._set_ip_text("https://a.trycloudflare.com")
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard._set_ip_text("https://b.trycloudflare.com")
        assert dashboard.ip_label.alignment() == Qt.AlignCenter


class TestAuthModeRestriction:
    """Cloudflare 模式强制 url_fragment：TOFU 选项禁用。"""

    def test_auth_tofu_enabled_in_lan_by_default(self, dashboard):
        assert dashboard.act_auth_tofu.isEnabled() is True

    def test_auth_tofu_disabled_in_cf_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_auth_tofu.isEnabled() is False

    def test_auth_tofu_re_enabled_back_to_lan(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._on_mode_clicked(TunnelMode.LAN)
        dashboard.on_switch_completed()
        assert dashboard.act_auth_tofu.isEnabled() is True

    def test_cf_with_tofu_config_shows_url_fragment_checked(self, dashboard):
        """配置为 tofu 时进入 CF 模式：TOFU 禁用 + url_fragment 勾选。"""
        dashboard._auth_method = "tofu"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_auth_tofu.isEnabled() is False
        assert dashboard.act_auth_url_fragment.isChecked() is True
        assert dashboard.act_auth_tofu.isChecked() is False

    def test_clicking_tofu_in_cf_mode_does_not_change_auth_method(self, dashboard):
        """CF 模式下点 TOFU 应被拒绝，配置不变。"""
        dashboard._auth_method = "url_fragment"
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        dashboard._sync_menu_checks()
        dashboard._on_auth_method_clicked("tofu")
        assert dashboard._auth_method == "url_fragment"
        assert dashboard.act_auth_url_fragment.isChecked() is True
        assert dashboard.act_auth_tofu.isChecked() is False


class TestAuthMethodToggle:
    """认证方式菜单只有 TOFU 手动审批 / 扫码认证 两项，加密永远开启。"""

    def test_menu_has_no_legacy_algo_items(self, dashboard):
        """不再暴露旧的加密/明文菜单项。"""
        assert not hasattr(dashboard, "act_algo_none")
        assert not hasattr(dashboard, "act_algo_encrypted")
        assert not hasattr(dashboard, "act_algo_xsalsa20")
        assert not hasattr(dashboard, "act_algo_xchacha20")

    def test_default_is_tofu(self, dashboard):
        """默认 TOFU 手动审批（LAN 模式）。"""
        assert dashboard.act_auth_tofu.isChecked() is True
        assert dashboard.act_auth_url_fragment.isChecked() is False

    def test_clicking_url_fragment_persists(self, dashboard):
        """点击扫码认证：配置写入 url_fragment。"""
        dashboard._on_auth_method_clicked("url_fragment")
        assert dashboard._auth_method == "url_fragment"
        assert dashboard.sm.get("auth_method") == "url_fragment"
        assert dashboard.act_auth_url_fragment.isChecked() is True
        assert dashboard.act_auth_tofu.isChecked() is False

    def test_clicking_tofu_back_in_lan(self, dashboard):
        """LAN 模式下可从扫码认证切回 TOFU。"""
        dashboard._on_auth_method_clicked("url_fragment")
        dashboard._on_auth_method_clicked("tofu")
        assert dashboard._auth_method == "tofu"
        assert dashboard.sm.get("auth_method") == "tofu"
        assert dashboard.act_auth_tofu.isChecked() is True

    def test_status_shows_auth_method_and_negotiated_algo(self, dashboard):
        """状态栏显示认证方式 + 协商出的算法。"""
        dashboard._auth_method = "tofu"
        dashboard.update_connection_status(True, "xchacha20")
        assert "XChaCha20" in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_auth_tofu") in dashboard.status_label.text()

    def test_status_url_fragment_with_algo(self, dashboard):
        """扫码认证模式下状态栏显示扫码 + 算法。"""
        dashboard._auth_method = "url_fragment"
        dashboard.update_connection_status(True, "xchacha20")
        assert "XChaCha20" in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_auth_url_fragment") in dashboard.status_label.text()

    def test_status_connected_without_negotiated_algo(self, dashboard):
        """未携带协商算法时只显示认证方式。"""
        dashboard._auth_method = "tofu"
        dashboard.update_connection_status(True)
        assert "XChaCha20" not in dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_auth_tofu") in dashboard.status_label.text()

    def test_status_negotiated_algo_cleared_on_disconnect(self, dashboard):
        """断开后清空协商结果。"""
        dashboard._auth_method = "tofu"
        dashboard.update_connection_status(True, "xchacha20")
        assert dashboard._negotiated_algo == "xchacha20"
        dashboard.update_connection_status(False)
        assert dashboard._negotiated_algo is None

    def test_algo_display_name_fallback_for_unknown_algo(self, dashboard):
        """locale 缺失的算法名回退为原始算法名（未来新增算法无需先加 locale）。"""
        assert dashboard._algo_display_name("xchacha20") == "XChaCha20"


class TestTofuApprovalUi:
    """TOFU 审批通知 UI 测试。"""

    def test_approval_frame_hidden_by_default(self, dashboard):
        """审批通知默认隐藏。"""
        assert dashboard._approval_frame.isVisible() is False

    def test_show_approval_request_displays_frame(self, dashboard, qtbot):
        """显示审批请求后通知框可见。"""
        with qtbot.waitExposed(dashboard):
            dashboard.show()
        dashboard.show_approval_request("1234", "192.168.1.100")
        assert dashboard._approval_frame.isVisible() is True

    def test_hide_approval_request_hides_frame(self, dashboard):
        """隐藏审批请求后通知框不可见。"""
        dashboard.show_approval_request("1234", "192.168.1.100")
        dashboard.hide_approval_request()
        assert dashboard._approval_frame.isVisible() is False

    def test_approval_callback_invoked_on_accept(self, dashboard):
        """点击允许后调用回调（参数为 True）。"""
        result = []
        dashboard.set_approval_callback(lambda approved: result.append(approved))
        dashboard.show_approval_request("5678", "10.0.0.1")
        dashboard._approval_accept_btn.click()
        assert result == [True]
        assert dashboard._approval_frame.isVisible() is False

    def test_approval_callback_invoked_on_deny(self, dashboard):
        """点击拒绝后调用回调（参数为 False）。"""
        result = []
        dashboard.set_approval_callback(lambda approved: result.append(approved))
        dashboard.show_approval_request("5678", "10.0.0.1")
        dashboard._approval_deny_btn.click()
        assert result == [False]
        assert dashboard._approval_frame.isVisible() is False


class TestRestartServiceMenu:
    """「网络」菜单底部的「重启服务」：两种模式都可用，与模式切换共用忙碌状态。"""

    def test_is_last_item_in_network_menu(self, dashboard):
        items = [a for a in dashboard.network_menu.actions() if not a.isSeparator()]
        assert items[-1] is dashboard.act_restart_service

    def test_enabled_in_lan_mode(self, dashboard):
        assert dashboard.get_mode() == TunnelMode.LAN
        assert dashboard.act_restart_service.isEnabled() is True

    def test_enabled_in_cf_mode(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()
        assert dashboard.act_restart_service.isEnabled() is True

    def test_trigger_calls_callback(self, dashboard):
        cb = MagicMock()
        dashboard.set_restart_service_callback(cb)
        dashboard.act_restart_service.trigger()
        cb.assert_called_once()

    def test_click_clears_stale_tunnel_url(self, dashboard):
        """CF 重启会换域名：旧地址必须清掉，否则失败时界面还显示已作废的二维码。"""
        dashboard._tunnel_url = "https://stale.trycloudflare.com"
        dashboard._on_restart_service()
        assert dashboard._tunnel_url is None
        assert "stale.trycloudflare.com" not in dashboard.ip_label.toPlainText()
        assert dashboard.ip_label.toPlainText() == dashboard.i18n.tr("dashboard.restarting")

    def test_busy_while_switching_then_re_enabled(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        assert dashboard.act_restart_service.isEnabled() is False
        assert dashboard.act_lan.isEnabled() is False
        assert dashboard.act_cf.isEnabled() is False

        dashboard.on_switch_completed()
        assert dashboard.act_restart_service.isEnabled() is True

    def test_second_click_while_busy_is_ignored(self, dashboard):
        """忙碌中再点一次只会让服务端白重启，必须挡住。"""
        cb = MagicMock()
        dashboard.set_restart_service_callback(cb)
        dashboard._on_restart_service()
        dashboard._on_restart_service()
        cb.assert_called_once()


class TestTunnelReachabilityStatus:
    """隧道失效提示挂在状态栏末尾，不弹托盘通知。

    保活每 60s 探测一次，偶发抖动若走通知会变成打扰；状态栏常驻、不抢焦点。
    """

    def _to_cf(self, dashboard):
        dashboard._on_mode_clicked(TunnelMode.CLOUDFLARE)
        dashboard.on_switch_completed()

    def test_lost_appends_suffix_in_cf_mode(self, dashboard):
        self._to_cf(dashboard)
        dashboard.set_tunnel_reachability(False)
        text = dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_disconnected") in text
        assert dashboard.i18n.tr("dashboard.tunnel_lost") in text

    def test_recovered_removes_suffix(self, dashboard):
        self._to_cf(dashboard)
        dashboard.set_tunnel_reachability(False)
        dashboard.set_tunnel_reachability(True)
        assert dashboard.i18n.tr("dashboard.tunnel_lost") not in dashboard.status_label.text()

    def test_suffix_keeps_connection_and_encryption_segments(self, dashboard):
        """失效段追加在最后，不吞掉前面已连接/加密方式的信息。"""
        self._to_cf(dashboard)
        dashboard.update_connection_status(True, algorithm="xchacha20")
        dashboard.set_tunnel_reachability(False)
        text = dashboard.status_label.text()
        assert dashboard.i18n.tr("dashboard.status_connected") in text
        assert "XChaCha20" in text
        assert dashboard.i18n.tr("dashboard.tunnel_lost") in text

    def test_lan_mode_never_shows_suffix(self, dashboard):
        """保活只在 CF 模式运行，局域网模式不应受这个状态影响。"""
        assert dashboard.get_mode() == TunnelMode.LAN
        dashboard.set_tunnel_reachability(False)
        assert dashboard.i18n.tr("dashboard.tunnel_lost") not in dashboard.status_label.text()

    def test_new_tunnel_url_clears_flag(self, dashboard, monkeypatch):
        """拿到新域名＝隧道刚重建，旧的失效标记必须作废。"""
        self._to_cf(dashboard)
        dashboard.set_tunnel_reachability(False)
        monkeypatch.setattr(dashboard, "_refresh_qr", lambda: None)  # 不依赖 QR 生成
        dashboard.update_tunnel_url("https://brand-new.trycloudflare.com")
        assert dashboard.i18n.tr("dashboard.tunnel_lost") not in dashboard.status_label.text()

    def test_mode_switch_clears_flag(self, dashboard):
        self._to_cf(dashboard)
        dashboard.set_tunnel_reachability(False)
        dashboard._on_mode_clicked(TunnelMode.LAN)
        dashboard.on_switch_completed()
        assert dashboard.i18n.tr("dashboard.tunnel_lost") not in dashboard.status_label.text()

    def test_restart_service_clears_flag(self, dashboard):
        """重启服务正是失效时的自救动作，先摘掉失效标记。"""
        self._to_cf(dashboard)
        dashboard.set_tunnel_reachability(False)
        dashboard._on_restart_service()
        assert dashboard.i18n.tr("dashboard.tunnel_lost") not in dashboard.status_label.text()

    def test_repeated_same_state_does_not_redraw(self, dashboard):
        """只有翻转才重绘：避免 60s 一轮的探测反复刷同一行。"""
        self._to_cf(dashboard)
        dashboard.status_label.setText("SENTINEL")
        dashboard.set_tunnel_reachability(True)  # 与初值相同，应直接返回
        assert dashboard.status_label.text() == "SENTINEL"
