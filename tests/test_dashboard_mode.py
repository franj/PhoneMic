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
    """TOFU 审批通知 UI 测试。

    界面是**快照的纯函数**：收到 ``show_approval_snapshot(items)`` 就整片重绘，
    只显示 ``items[0]``，按钮回调带的是那条的 **id**。因此"看到的识别码"与
    "按下去的请求"必然同源——这是本类里最要紧的一条契约（见
    ``test_switch_to_next_item_redraws_pin`` 与 ``test_click_targets_displayed_id``）。
    """

    @pytest.fixture
    def shown(self, dashboard, qtbot):
        """已显示的 dashboard：子控件的 isVisible/几何只有父窗口显示后才有意义。"""
        with qtbot.waitExposed(dashboard):
            dashboard.show()
        qtbot.wait(50)      # 让布局跑完一轮，几何才稳定
        return dashboard

    @staticmethod
    def _snap(*pairs):
        """构造服务端下发的快照（新的在前）。pairs 形如 ("3847", "10.0.0.7")。"""
        return [
            # remaining：服务端给的剩余秒数，界面据此画标题里的倒计时
            {"id": f"req-{i}", "pin": pin, "ip": ip, "remaining": 30}
            for i, (pin, ip) in enumerate(pairs)
        ]

    def test_approval_frame_hidden_by_default(self, dashboard):
        """审批通知默认隐藏。"""
        assert dashboard._approval_frame.isVisible() is False

    def test_snapshot_displays_frame(self, shown):
        """收到非空快照后通知框可见。"""
        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))
        assert shown._approval_frame.isVisible() is True

    def test_empty_snapshot_hides_frame(self, shown):
        """空快照即收起——「撤销」也是快照的一种，不另发隐藏事件。"""
        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))
        shown.show_approval_snapshot([])
        assert shown._approval_frame.isVisible() is False

    def test_hide_approval_request_hides_frame(self, shown):
        """隐藏审批请求后通知框不可见。"""
        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))
        shown.hide_approval_request()
        assert shown._approval_frame.isVisible() is False

    def test_pin_shown_digit_by_digit(self, shown):
        """识别码逐位分隔显示——便于与手机屏幕上的数字逐一核对。"""
        shown.show_approval_snapshot(self._snap(("3847", "192.168.1.100")))
        assert shown._approval_pin_label.text() == "3 8 4 7"

    def test_pin_font_is_large_and_bold(self, shown):
        """识别码字号明显大于正文（审批场景下要一眼可读）。"""
        assert shown._approval_pin_label.font().pointSize() >= 24
        assert shown._approval_pin_label.font().bold() is True
        assert shown._approval_pin_label.font().pointSize() > shown.ip_label.font().pointSize()

    def test_approval_replaces_ip_and_info_block(self, shown):
        """审批通知与地址栏/说明区同位互斥：显示时让位，隐藏时归还。"""
        assert shown.ip_label.isVisible() is True

        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))
        assert shown.ip_label.isVisible() is False
        assert shown.info_label.isVisible() is False
        assert shown.cf_info_label.isVisible() is False
        assert shown._approval_frame.isVisible() is True

        shown.hide_approval_request()
        assert shown.ip_label.isVisible() is True
        assert shown.info_label.isVisible() is True
        assert shown._approval_frame.isVisible() is False

    def test_info_label_restored_by_mode_after_approval(self, shown):
        """让位后的归还按当前模式还原：CF 模式还回 CF 说明而非局域网说明。"""
        shown._on_mode_clicked(TunnelMode.CLOUDFLARE)
        shown.on_switch_completed()
        shown._sync_menu_checks()

        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))
        assert shown.cf_info_label.isVisible() is False

        shown.hide_approval_request()
        assert shown.cf_info_label.isVisible() is True
        assert shown.info_label.isVisible() is False

    def test_mode_change_keeps_info_hidden_while_approving(self, shown):
        """审批进行中刷新界面（模式切换等路径会走 _apply_mode_ui）不得让说明标签回位。

        否则说明标签会与审批面板同时占住同一块位置，把面板挤下去。
        """
        shown.show_approval_snapshot(self._snap(("1234", "192.168.1.100")))

        shown._apply_mode_ui()

        assert shown.info_label.isVisible() is False
        assert shown.cf_info_label.isVisible() is False
        assert shown.ip_label.isVisible() is False
        assert shown._approval_frame.isVisible() is True

    def test_ip_shown_in_approval_panel(self, shown):
        """来源 IP 仍是核对凭据之一（识别码之外的辅助信息）。"""
        shown.show_approval_snapshot(self._snap(("1234", "10.0.0.7")))
        assert "10.0.0.7" in shown._approval_info.text()

    def test_approval_panel_fits_fixed_window(self, shown):
        """审批面板必须完整放得下——主界面尺寸固定，放不下会静默裁掉内容。

        这是几何验收而非显隐断言：setFixedSize 下父布局空间不足时会把控件压到
        最小尺寸，控件仍处于"可见"状态，内容却已被裁掉（用户看到半个按钮）。

        判据用 minimumHeight 而不是 sizeHint：面板高度已由
        ``QLayout.SetMinimumSize`` 钉住，minimumHeight 才是确定性的下限。

        用 **3 条**的满配形态验收：并发提示文字与「全部拒绝」按钮都要出现——
        它们只在最需要显示的时候才占位置，也正是最容易把面板撑爆的时候。
        """
        shown.show_approval_snapshot(self._snap(
            ("3847", "192.168.1.100"),
            ("1234", "192.168.1.101"),
            ("5678", "192.168.1.102"),
        ))
        frame = shown._approval_frame

        assert frame.height() >= frame.minimumHeight()
        for btn in (shown._approval_accept_btn, shown._approval_deny_btn,
                    shown._approval_deny_all_btn):
            assert btn.isVisible() is True
            assert btn.height() >= btn.sizeHint().height()

        # 识别码、说明、风险提示都不换行：宽度不够就会被水平裁掉，必须逐个守住
        for label in (shown._approval_pin_label, shown._approval_info,
                      shown._approval_title, shown._approval_risk_label):
            assert label.width() >= label.sizeHint().width(), label.text()

        # 面板底边仍在窗口内容区内（不越过中央控件边界）
        bottom = frame.mapTo(shown.centralWidget(), frame.rect().bottomLeft()).y()
        assert bottom <= shown.centralWidget().height()

    def test_callback_receives_request_id_on_accept(self, shown):
        """点击允许：回调收到 (id, True)——id 是**当前显示**那条的。"""
        result = []
        shown.set_approval_callback(lambda rid, ok: result.append((rid, ok)))
        shown.show_approval_snapshot(self._snap(("5678", "10.0.0.1")))
        shown._approval_accept_btn.click()
        assert result == [("req-0", True)]

    def test_callback_receives_request_id_on_deny(self, shown):
        """点击拒绝：回调收到 (id, False)。"""
        result = []
        shown.set_approval_callback(lambda rid, ok: result.append((rid, ok)))
        shown.show_approval_snapshot(self._snap(("5678", "10.0.0.1")))
        shown._approval_deny_btn.click()
        assert result == [("req-0", False)]

    def test_panel_waits_for_server_snapshot_after_click(self, shown):
        """点击后界面**不自行撤下面板**：撤下由服务端推来的快照决定。

        否则会出现「已经点了允许、面板没了、连接却没建立」的无反馈状态——用户
        只能靠猜。服务端结算后立刻推新快照（下一条或空），界面照它画即可。
        """
        shown.set_approval_callback(lambda rid, ok: None)
        shown.show_approval_snapshot(self._snap(("5678", "10.0.0.1")))
        shown._approval_accept_btn.click()

        assert shown._approval_frame.isVisible() is True

        shown.show_approval_snapshot([])          # 服务端结算后推来的空快照
        assert shown._approval_frame.isVisible() is False


class TestApprovalQueue:
    """队列语义：只显示一条、结算后补位、并发过多时给提示与「全部拒绝」。"""

    @pytest.fixture
    def shown(self, dashboard, qtbot):
        with qtbot.waitExposed(dashboard):
            dashboard.show()
        qtbot.wait(50)
        return dashboard

    @staticmethod
    def _snap(*pairs):
        return [
            # remaining：服务端给的剩余秒数，界面据此画标题里的倒计时
            {"id": f"req-{i}", "pin": pin, "ip": ip, "remaining": 30}
            for i, (pin, ip) in enumerate(pairs)
        ]

    def test_head_is_displayed_when_queue_has_more(self, shown):
        """队列里有多条时只显示队首（新的在前），标题给出总数。"""
        shown.show_approval_snapshot(self._snap(
            ("1111", "10.0.0.1"), ("2222", "10.0.0.2"), ("3333", "10.0.0.3")))

        assert shown._approval_pin_label.text() == "1 1 1 1"
        assert "10.0.0.1" in shown._approval_info.text()
        assert "3" in shown._approval_title.text()

    def test_switch_to_next_item_redraws_pin(self, shown):
        """结算一条后补位显示下一条——**识别码必须整片重绘**。

        4 位数字只要漏重绘一次就会残留成上一条的值，而用户是照着屏幕上的数字去
        和手机核对的：那会让用户为"上一条的码"批准"下一条的请求"。这条是验收点。
        """
        shown.show_approval_snapshot(self._snap(
            ("3847", "10.0.0.1"), ("5017", "10.0.0.2")))
        assert shown._approval_pin_label.text() == "3 8 4 7"

        # req-0 被别处结算（超时/掉线/用户点掉），服务端推来只剩 req-1 的快照
        shown.show_approval_snapshot(
            [{"id": "req-1", "pin": "5017", "ip": "10.0.0.2"}])

        assert shown._approval_pin_label.text() == "5 0 1 7"
        assert "10.0.0.2" in shown._approval_info.text()

    def test_click_targets_displayed_id(self, shown):
        """按钮作用的 id 必须与屏幕上显示的识别码出自同一份快照。"""
        result = []
        shown.set_approval_callback(lambda rid, ok: result.append((rid, ok)))
        shown.show_approval_snapshot(self._snap(
            ("3847", "10.0.0.1"), ("5017", "10.0.0.2")))
        # 队首换人（req-0 已被结算）之后用户才点的按钮
        shown.show_approval_snapshot(
            [{"id": "req-1", "pin": "5017", "ip": "10.0.0.2"}])

        shown._approval_deny_btn.click()

        assert result == [("req-1", False)], "必须结算当前显示的那条（req-1）"

    def test_no_risk_hint_below_threshold(self, shown):
        """两条并发还不算异常（手机重连 + 旧页面残留），不吓唬用户。"""
        shown.show_approval_snapshot(self._snap(
            ("1111", "10.0.0.1"), ("2222", "10.0.0.2")))

        assert shown._approval_risk_label.isVisible() is False
        assert shown._approval_deny_all_btn.isVisible() is False

    def test_risk_hint_and_deny_all_at_threshold(self, shown):
        """达到阈值：出提示文字 + 「全部拒绝」，且**不是弹框**。

        弹框会骚扰根本没在用程序的人（用户离开电脑时突然蹦一个框），而这条信息
        只对"正在看界面的人"有意义——主界面面板常驻，回来一眼就能看到。
        """
        shown.show_approval_snapshot(self._snap(
            ("1111", "10.0.0.1"), ("2222", "10.0.0.2"), ("3333", "10.0.0.3")))

        assert shown._approval_risk_label.isVisible() is True
        assert shown._approval_risk_label.text() != ""
        assert shown._approval_deny_all_btn.isVisible() is True

    def test_risk_hint_disappears_when_queue_shrinks(self, shown):
        """队列缩回阈值以下：提示与按钮一起收回，不留残影。"""
        shown.show_approval_snapshot(self._snap(
            ("1111", "10.0.0.1"), ("2222", "10.0.0.2"), ("3333", "10.0.0.3")))
        shown.show_approval_snapshot(self._snap(("1111", "10.0.0.1")))

        assert shown._approval_risk_label.isVisible() is False
        assert shown._approval_deny_all_btn.isVisible() is False

    def test_deny_all_sends_none_id(self, shown):
        """「全部拒绝」用 id=None 表达：一个决定，不是逐条模拟点击。"""
        result = []
        shown.set_approval_callback(lambda rid, ok: result.append((rid, ok)))
        shown.show_approval_snapshot(self._snap(
            ("1111", "10.0.0.1"), ("2222", "10.0.0.2"), ("3333", "10.0.0.3")))

        shown._approval_deny_all_btn.click()

        assert result == [(None, False)]

    def test_click_without_snapshot_does_nothing(self, shown):
        """队列为空时的点击是空操作（面板已收起，不该发出无 id 的结算）。"""
        result = []
        shown.set_approval_callback(lambda rid, ok: result.append((rid, ok)))

        shown._resolve_approval(True)

        assert result == []


class TestApprovalCountdown:
    """标题里的倒计时：**界面只负责显示**，超时的权威始终在服务端。"""

    @pytest.fixture
    def shown(self, dashboard, qtbot):
        with qtbot.waitExposed(dashboard):
            dashboard.show()
        qtbot.wait(50)
        return dashboard

    @staticmethod
    def _one(remaining=30):
        """一条带剩余秒数的快照（服务端下发的相对时长）。"""
        return [{"id": "req-1", "pin": "1234", "ip": "10.0.0.1",
                 "remaining": remaining}]

    def test_title_shows_seconds_from_snapshot(self, shown):
        """标题行带上快照给的剩余秒数。"""
        shown.show_approval_snapshot(self._one(30))

        assert "30" in shown._approval_title.text()

    def test_countdown_ticks_down_without_new_snapshot(self, shown):
        """快照只在状态变更时下发，中间这一段秒数必须由界面自己走。

        否则倒计时会一直停在快照到达的那一刻（30 → 30 → 30），比不显示更误导。
        """
        shown.show_approval_snapshot(self._one(30))
        # 把本地锚点往前拨 7 秒，等价于"这份快照已经到手 7 秒了"
        shown._approval_anchor -= 7
        shown._tick_approval_countdown()

        assert "23" in shown._approval_title.text()
        assert "30" not in shown._approval_title.text()

    def test_countdown_never_negative(self, shown):
        """界面钟比服务端快时秒数到 0 为止，不出现负数。"""
        shown.show_approval_snapshot(self._one(5))
        shown._approval_anchor -= 99
        shown._tick_approval_countdown()

        assert shown._approval_seconds_left() == 0
        assert "-" not in shown._approval_title.text()

    def test_countdown_zero_does_not_hide_panel(self, shown):
        """**倒计时归零不撤面板**，也不发任何结算。

        撤下只能由服务端推来的空快照决定。界面自己撤会造出"面板没了、服务端还在
        等"的错位——用户想点「允许」时按钮已经不在，只能重连。这条是本类存在的
        理由：时钟可以有，权力不能有。
        """
        called = []
        shown.set_approval_callback(lambda rid, ok: called.append((rid, ok)))
        shown.show_approval_snapshot(self._one(3))
        shown._approval_anchor -= 99
        shown._tick_approval_countdown()

        assert called == [], "倒计时归零不是一次结算"
        assert shown._approval_frame.isVisible() is True
        # 面板还开着，用户此刻点「允许」仍然作用在原来那条上
        assert shown._approval_items[0]["id"] == "req-1"

    def test_timer_keeps_running_at_zero(self, shown):
        """归零只是停表（省掉无意义刷新），不是撤面板——面板仍在。"""
        shown.show_approval_snapshot(self._one(1))
        shown._approval_anchor -= 99
        shown._tick_approval_countdown()

        assert shown._approval_timer.isActive() is False
        assert shown._approval_frame.isVisible() is True

    def test_without_remaining_field_no_countdown(self, shown):
        """快照没带剩余秒数时退化成纯标题，不显示一个凭空的 0。"""
        shown.show_approval_snapshot([{"id": "req-1", "pin": "1234", "ip": "10.0.0.1"}])

        assert shown._approval_title.text() == shown.i18n.tr("dashboard.approval_title")
        assert shown._approval_timer.isActive() is False

    def test_timer_stops_when_panel_hides(self, shown):
        """空快照收起面板时定时器要停，别让它在后台空转。"""
        shown.show_approval_snapshot(self._one(30))
        assert shown._approval_timer.isActive() is True

        shown.show_approval_snapshot([])

        assert shown._approval_timer.isActive() is False

    def test_new_snapshot_resets_countdown(self, shown):
        """补位到下一条时倒计时跟着重置（剩余秒数来自新那条自己的 deadline）。"""
        shown.show_approval_snapshot(self._one(30))
        shown._approval_anchor -= 25                       # 秒数走到 5
        assert shown._approval_seconds_left() == 5

        shown.show_approval_snapshot(
            [{"id": "req-2", "pin": "5678", "ip": "10.0.0.2", "remaining": 30}])

        assert shown._approval_seconds_left() == 30
        assert "5678" not in shown._approval_title.text()   # 识别码只在场内大字里
        assert shown._approval_pin_label.text() == "5 6 7 8"

    @pytest.mark.parametrize("locale", ["zh_CN", "zh_HK", "zh_TW", "en_US"])
    def test_title_fits_in_every_locale(self, shown, monkeypatch, locale):
        """倒计时并进标题行后，**4 个语言包都得放得下**（3 条满配是最挤的形态）。

        ``test_approval_panel_fits_fixed_window`` 只跑当前系统语言，而最宽的是
        en_US——"标题够不够宽"这件事必须逐个语言包守住，否则某个译文会悄悄撑破、
        被固定尺寸窗口静默裁掉，只有那个语言的用户看得见。
        """
        import json
        from phonemic.utils.paths import get_res_path

        with open(get_res_path(f"locales/{locale}.json"), encoding="utf-8") as fh:
            monkeypatch.setattr(shown.i18n, "_strings", json.load(fh))

        shown.show_approval_snapshot([
            {"id": f"req-{i}", "pin": pin, "ip": ip, "remaining": 30}
            for i, (pin, ip) in enumerate((
                ("3847", "192.168.1.100"),
                ("1234", "192.168.1.101"),
                ("5678", "192.168.1.102")))
        ])

        label = shown._approval_title
        assert "30" in label.text(), label.text()
        assert label.width() >= label.sizeHint().width(), (
            f"[{locale}] 标题被裁：{label.text()!r}")


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
