from typing import Callable, Optional
import subprocess
import sys
import time

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics, QPixmap, QAction, QActionGroup, QPainter, QColor, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QFrame, QMessageBox, QLayout,
    QApplication, QDialog, QRadioButton, QCheckBox, QDialogButtonBox
)

from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.gui.commands_dialog import CommandsDialog
from phonemic.tunnel.mode import TunnelMode, get_mode, set_mode, effective_auth_method
from phonemic.utils.paths import get_app_root, get_build_info, is_frozen
from phonemic.utils.i18n import I18n
from phonemic.utils.settings_manager import SettingsManager


# 并发到几条就该提醒用户「可能有风险」。3 是这么定的：正常用法下最多出现 2 条
# （手机重连 + 旧页面残留），同 IP 的新连接还会把旧的那条顶掉，所以 3 条意味着
# 至少来自两个不同来源——这时才值得把注意力从"核对识别码"升到"改用扫码认证"。
APPROVAL_RISK_THRESHOLD = 3


def make_qr_pixmap(data: str, size: int = 250) -> QPixmap:
    """直接从 qrcode 矩阵生成 QPixmap，不依赖 PIL/Pillow。"""
    qr = qrcode.QRCode(box_size=1, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    matrix_size = len(matrix)
    scale = max(1, size // matrix_size)
    actual_size = matrix_size * scale

    pixmap = QPixmap(actual_size, actual_size)
    pixmap.fill(Qt.white)

    painter = QPainter(pixmap)
    painter.setBrush(QColor(0, 0, 0))
    painter.setPen(Qt.NoPen)
    for y, row in enumerate(matrix):
        for x, is_dark in enumerate(row):
            if is_dark:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()

    if pixmap.width() != size or pixmap.height() != size:
        pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pixmap


class Dashboard(QMainWindow):
    # ===== 改动：构造函数增加 tray 参数（可选）=====
    def __init__(self, ip: str, port: int, tray=None, parent=None):
        super().__init__(parent)
        self.i18n = I18n.instance()
        self.sm = SettingsManager.instance()
        self.tray = tray  # 保存托盘对象引用
        self.setWindowTitle(self.i18n.tr("dashboard.title"))
        self.setFixedSize(400, 520)
        self.setWindowFlags(self.windowFlags() & (~Qt.WindowMaximizeButtonHint) | Qt.WindowCloseButtonHint)
        self._mode: TunnelMode = get_mode()
        self._tunnel_url: Optional[str] = None
        self._qr_url: Optional[str] = f"http://{ip}:{port}"
        self._lan_ip = ip
        self._lan_port = port
        self._switching = False
        self._mode_switch_callback: Optional[Callable[[TunnelMode], None]] = None
        self._restart_service_callback: Optional[Callable[[], None]] = None
        self._secure_channel = None  # SecureChannel 引用，由外部设置
        self._auth_method: str = self.sm.get("auth_method", "tofu")
        self._negotiated_algo: Optional[str] = None  # 本次连接握手协商出的算法，由 connect 事件携带
        self._algorithm_change_callback: Optional[Callable[[str], None]] = None
        # TOFU 审批：界面侧只保存"最近一次快照"这一份不可变投影，自己不维护队列
        # 状态——漏事件、乱序重绘都能靠下一次全量快照自愈（与 mobile.html 的
        # SignalHub 同哲学）。按钮作用的那条请求 = _approval_items[0]。
        self._approval_items: list = []
        self._approval_callback: Optional[Callable[[Optional[str], bool], None]] = None
        # 倒计时的两个入参：快照到达时的剩余秒数（服务端给的），与本地 monotonic
        # 锚点。界面**不持有 deadline**——超时的权威在服务端，这里只把"还剩多久"
        # 画出来，快照给的也是相对时长而非绝对时刻（绝对时刻跨进程就没意义了）。
        self._approval_remaining: Optional[int] = None
        self._approval_anchor: float = 0.0
        # connection 状态栏的两个入参：connected 此前只在 update_connection_status()
        # 里赋值（构造函数没给初值），任何在首次调用前读取 self.connected 的路径
        # 都会 AttributeError；这里补上 false，让重绘入口可以无条件自刷新。
        self.connected = False
        # 隧道公网入口可达性（保活探测结果，见 TunnelKeepalive）。True＝可达/未知。
        self._tunnel_reachable: bool = True
        self._setup_ui(ip, port)
        self._setup_menu()
        self._apply_mode_ui()
        # 上屏方式可能被偏好设置 / 托盘菜单改动，同步本菜单勾选状态
        self.sm.connect_changed("text_input_mode", self._on_input_mode_setting_changed)

    def set_restart_network_callback(self, callback):
        """设置切换网络的回调函数"""
        self._restart_network_callback = callback

    def set_mode_switch_callback(self, callback: Callable[[TunnelMode], None]):
        """设置模式切换回调函数"""
        self._mode_switch_callback = callback

    def set_restart_service_callback(self, callback: Callable[[], None]):
        """设置「重启服务」回调函数"""
        self._restart_service_callback = callback

    def set_secure_channel(self, sc):
        """设置安全通道引用，并刷新 QR 码以包含公钥。"""
        self._secure_channel = sc
        self._refresh_qr()

    def set_algorithm_change_callback(self, callback: Callable[[str], None]):
        """设置认证方式变更回调函数。"""
        self._algorithm_change_callback = callback

    def set_approval_callback(self, callback: Callable[[Optional[str], bool], None]):
        """设置 TOFU 审批结果回调。

        回调签名 ``(request_id, approved)``：``request_id`` 是**界面当前显示那条**
        请求的 id，服务端据此结算那一条——于是「用户看到的识别码」与「被结算的
        请求」必然出自同一份快照。``request_id=None`` 且 ``approved=False`` 表示
        「全部拒绝」（只有并发 ≥ APPROVAL_RISK_THRESHOLD 时界面才给这个入口）。
        """
        self._approval_callback = callback

    def show_approval_snapshot(self, items) -> None:
        """按全量快照重绘审批区（``items`` 为空即收起）。

        快照来自服务端，形如 ``[{"id","pin","ip"}, ...]``，**新的在前**，界面只
        显示第一条。整片重绘而不是"只替换变了的字段"：识别码是从上一条继承下来
        的残留值，而用户是照着屏幕上的数字去和手机核对的——只要漏重绘一次，
        就会出现「看到的码」与「按下去的请求」不是同一条，这种错位恰好能骗过
        用户（也正是把 4 位数字拉成 30pt 的理由，不能让它是旧值）。
        """
        items = [it for it in (items or []) if isinstance(it, dict)]
        if not items:
            self.hide_approval_request()
            return
        self._approval_items = items

        head = items[0]
        total = len(items)
        # 倒计时起点：快照给的剩余秒数 + 本地锚点，之后由 _approval_timer 每秒重画。
        # 只在快照同时带了队列与秒数时才动定时器——缺字段（旧版后端、测试构造的
        # 极简快照）就退化成"没有倒计时"，而不是显示一个凭空的 0。
        remaining = head.get("remaining")
        self._approval_remaining = remaining if isinstance(remaining, int) else None
        self._approval_anchor = time.monotonic()
        # 队列长度只体现在标题上（"认证请求（共 3 条）"）：界面永远只显示队首那条，
        # 不给"上一条/下一条"的假翻页入口——按钮作用的那条必须和显示的那条一致。
        self._refresh_approval_title()
        if self._approval_remaining is None:
            self._approval_timer.stop()
        else:
            self._approval_timer.start()
        pin = head.get("pin") or ""
        # 逐位空格分隔：隔着距离也能一眼念出，便于与手机屏幕上的数字逐一核对
        self._approval_pin_label.setText(" ".join(pin) if pin else "----")
        self._approval_info.setText(
            self.i18n.tr("dashboard.approval_detail", ip=head.get("ip") or "?"))
        self._approval_accept_btn.setText(self.i18n.tr("dashboard.approval_accept"))
        self._approval_deny_btn.setText(self.i18n.tr("dashboard.approval_deny"))

        # 并发过多：不弹框，只在面板里加一行醒目文字 + 一个「全部拒绝」。
        # 弹框会骚扰根本没在用程序的人（对用户是纯打扰），而这条信息只对"正在
        # 看界面的人"有意义；面板是常驻的，用户回来一眼能看到。
        risky = total >= APPROVAL_RISK_THRESHOLD
        self._approval_deny_all_btn.setVisible(risky)
        self._approval_risk_label.setVisible(risky)
        if risky:
            self._approval_risk_label.setText(self.i18n.tr("dashboard.approval_risk"))
            self._approval_deny_all_btn.setText(
                self.i18n.tr("dashboard.approval_deny_all"))

        self._set_main_info_visible(False)
        self._approval_frame.setVisible(True)
        self._approval_frame.update()

    def hide_approval_request(self) -> None:
        """隐藏审批通知，并把地址栏/说明区还回来。"""
        self._approval_items = []
        self._approval_timer.stop()
        self._approval_remaining = None
        self._approval_frame.setVisible(False)
        self._set_main_info_visible(True)

    def _approval_seconds_left(self) -> Optional[int]:
        """按本地锚点推算当前该显示的剩余秒数；快照没给秒数时返回 None。"""
        if self._approval_remaining is None:
            return None
        elapsed = time.monotonic() - self._approval_anchor
        return max(0, self._approval_remaining - int(elapsed))

    def _refresh_approval_title(self) -> None:
        """重画标题：队列长度 + 剩余秒数。

        倒计时并进标题行而不是新起一个控件：面板高度受主界面固定高度约束，多一行
        就多一份被静默裁掉的风险（契约见 test_approval_panel_fits_fixed_window）。
        """
        total = len(self._approval_items)
        if total > 1:
            text = self.i18n.tr("dashboard.approval_title_multi", total=total)
        else:
            text = self.i18n.tr("dashboard.approval_title")
        seconds = self._approval_seconds_left()
        if seconds is not None:
            text = "{} · {}".format(
                text, self.i18n.tr("dashboard.approval_countdown", seconds=seconds))
        self._approval_title.setText(text)

    def _tick_approval_countdown(self) -> None:
        """每秒重画倒计时；归零即停表，但**不撤面板**。

        停表只是省掉无意义的刷新；真正的收尾（超时结算 + 推空快照）在服务端。界面
        自己撤面板会造出"面板没了、服务端还在等"的错位——用户想点「允许」时按钮
        已经不在，只能重连。
        """
        if not self._approval_items:
            self._approval_timer.stop()
            return
        self._refresh_approval_title()
        if self._approval_seconds_left() == 0:
            self._approval_timer.stop()

    def _set_main_info_visible(self, visible: bool) -> None:
        """审批通知与地址栏/说明区互斥显示。

        让位时两者都藏起来（含 CF 说明），恢复时按当前模式还原应有的那一条——
        与 _apply_mode_ui 的显隐规则保持一致，但不依赖它（它会在切换中提前返回）。
        """
        self.ip_label.setVisible(visible)
        if not visible:
            self.info_label.setVisible(False)
            self.cf_info_label.setVisible(False)
            return
        is_lan = self._mode == TunnelMode.LAN
        self.info_label.setVisible(is_lan)
        self.cf_info_label.setVisible(not is_lan)

    def _resolve_approval(self, approved: bool) -> None:
        """把用户对这一条（= 当前显示的队首）的决定交给后端。

        面板的撤下**不在这里做**：服务端结算后会立刻推一份新的全量快照（通常换成
        队列里下一条，或空快照收起），界面只是快照的函数。这样"谁被批准了"永远
        由服务端说了算，界面不会出现「已经点了接受、面板没了、连接却没建立」的
        无反馈状态。
        """
        items = self._approval_items
        if not items:
            return
        cb = self._approval_callback
        if cb:
            cb(items[0].get("id"), approved)

    def _resolve_approval_all(self) -> None:
        """「全部拒绝」：id=None 表示作用于队列里所有待审批请求。"""
        cb = self._approval_callback
        if cb:
            cb(None, False)

    def set_mouse_debug_window(self, win):
        """注入独立调试窗口（临时工具），由「程序」菜单打开。"""
        self._mouse_debug_win = win

    def _open_mouse_debug(self):
        win = getattr(self, "_mouse_debug_win", None)
        if win:
            win.show()
            win.raise_()
            win.activateWindow()

    def _setup_ui(self, ip, port) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # ----- 二维码 -----
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)

        qr_url = f"http://{ip}:{port}"
        pixmap = make_qr_pixmap(qr_url)
        qr_label.setPixmap(pixmap)
        layout.addWidget(qr_label)

        # ----- 分隔线 -----
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        # 地址栏：QTextBrowser —— 只读、无边框、无滚动条、按字符换行。
        # 长 URL 保证完整显示，且高度固定，不会像 wordWrap 的 QLabel 那样
        # 被布局压扁裁掉、也不会撑开去挤别的地方。
        self.ip_label = QTextBrowser()
        self.ip_label.setWordWrapMode(QTextOption.WrapAnywhere)   # 关键：不按词、按字符断
        self.ip_label.setFrameShape(QFrame.NoFrame)
        self.ip_label.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ip_label.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.ip_label.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.ip_label.document().setDocumentMargin(0)
        fm = QFontMetrics(self.ip_label.font())
        self.ip_label.setFixedHeight(fm.lineSpacing() * 3 + 4)     # 固定 3 行高，可调
        self._set_ip_text(f"http://{ip}:{port}")
        layout.addWidget(self.ip_label)

        # ----- TOFU 审批通知（主界面内嵌，非弹窗）-----
        # 与地址栏/说明区**同位**：审批期间把 ip_label 与说明标签整块让给审批通知
        # （见 _set_main_info_visible），既不把主界面撑高，也让注意力落在识别码上。
        self._approval_frame = QFrame()
        self._approval_frame.setFrameShape(QFrame.Box)
        self._approval_frame.setStyleSheet(
            "QFrame { border: 2px solid #4CAF50; border-radius: 6px; background: #f1f8e9; }")
        self._approval_frame.setVisible(False)
        approval_layout = QVBoxLayout(self._approval_frame)
        approval_layout.setContentsMargins(4, 4, 4, 4)
        approval_layout.setSpacing(3)
        # 主界面尺寸固定（setFixedSize），审批面板要靠"最小尺寸"把高度钉住：
        # 否则父布局空间不足时会静默压扁它，按钮/识别码被裁掉一半。
        approval_layout.setSizeConstraint(QLayout.SetMinimumSize)

        self._approval_title = QLabel()
        self._approval_title.setAlignment(Qt.AlignCenter)
        self._approval_title.setStyleSheet(
            "font-weight: bold; color: #2e7d32; font-size: 12px; border: none;")
        approval_layout.addWidget(self._approval_title)

        # 识别码：审批场景下唯一需要用户"读出来核对"的信息。字号拉到 30pt（正文
        # 的约 4 倍）+ 加粗 + 逐位空格分隔，隔着一段距离也能一眼念出。
        # 面板总高受主界面固定高度约束——它要正好塞进地址栏+说明区让出的空间，
        # 因此说明文字必须单行（见 locales 的 approval_detail 长度）。
        self._approval_pin_label = QLabel()
        self._approval_pin_label.setAlignment(Qt.AlignCenter)
        pin_font = self._approval_pin_label.font()
        pin_font.setPointSize(30)
        pin_font.setBold(True)
        self._approval_pin_label.setFont(pin_font)
        self._approval_pin_label.setStyleSheet("color: #1b5e20; border: none;")
        approval_layout.addWidget(self._approval_pin_label)

        self._approval_info = QLabel()
        self._approval_info.setAlignment(Qt.AlignCenter)
        # 刻意不换行：换行会让面板高度随可用宽度浮动（heightForWidth 不透过
        # QFrame 传递），固定高度窗口下难以保证不被裁。文案长度由 locales 控制，
        # 契约由 test_approval_panel_fits_fixed_window 的宽度断言守住。
        self._approval_info.setStyleSheet("color: #33691e; font-size: 11px; border: none;")
        approval_layout.addWidget(self._approval_info)

        # 风险提示：并发请求过多时出现（默认隐藏）。用红色小字而不是弹框——弹框
        # 会打扰根本没在用程序的人，而这里的信息只对"正在看界面的人"有意义。
        # 同样不换行，宽度契约由上一条测试一并守住。
        self._approval_risk_label = QLabel()
        self._approval_risk_label.setAlignment(Qt.AlignCenter)
        self._approval_risk_label.setStyleSheet(
            "color: #c62828; font-size: 10px; font-weight: bold; border: none;")
        self._approval_risk_label.setVisible(False)
        approval_layout.addWidget(self._approval_risk_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._approval_accept_btn = QPushButton()
        self._approval_accept_btn.setMinimumHeight(28)
        self._approval_accept_btn.setStyleSheet(
            "QPushButton { background: #4CAF50; color: white; border: none; "
            "border-radius: 4px; font-size: 13px; font-weight: bold; }")
        self._approval_accept_btn.clicked.connect(lambda: self._resolve_approval(True))
        btn_row.addWidget(self._approval_accept_btn)

        self._approval_deny_btn = QPushButton()
        self._approval_deny_btn.setMinimumHeight(28)
        self._approval_deny_btn.setStyleSheet(
            "QPushButton { background: #e53935; color: white; border: none; "
            "border-radius: 4px; font-size: 13px; }")
        self._approval_deny_btn.clicked.connect(lambda: self._resolve_approval(False))
        btn_row.addWidget(self._approval_deny_btn)

        # 「全部拒绝」：只在并发过多时出现（见 APPROVAL_RISK_THRESHOLD）。
        # 给按钮而不是自动清理——自动拒绝有安全风险（可能误伤用户自己的第二条
        # 设备），让用户明确表达"这些我都不认"。
        self._approval_deny_all_btn = QPushButton()
        self._approval_deny_all_btn.setMinimumHeight(28)
        self._approval_deny_all_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; border: none; "
            "border-radius: 4px; font-size: 12px; }")
        self._approval_deny_all_btn.setVisible(False)
        self._approval_deny_all_btn.clicked.connect(self._resolve_approval_all)
        btn_row.addWidget(self._approval_deny_all_btn)
        approval_layout.addLayout(btn_row)

        layout.addWidget(self._approval_frame)

        # 倒计时表：每秒重画一次标题里的剩余秒数。**纯观感**——它归零时界面什么都
        # 不做（不撤面板、不发结算），撤下只能由服务端推来的空快照决定。这是"界面
        # 是快照的纯函数"这条原则的边界：时钟可以有，权力不能有。
        self._approval_timer = QTimer(self)
        self._approval_timer.setInterval(1000)
        self._approval_timer.timeout.connect(self._tick_approval_countdown)

        # ----- Cloudflare 说明（仅 Cloudflare 模式可见）-----
        self.cf_info_label = QLabel(self.i18n.tr("dashboard.cf_info"))
        self.cf_info_label.setAlignment(Qt.AlignCenter)
        self.cf_info_label.setWordWrap(True)
        self.cf_info_label.setStyleSheet("color: blue; font-size: 10px;")
        self.cf_info_label.setVisible(False)
        layout.addWidget(self.cf_info_label)

        info_label = QLabel(self.i18n.tr("dashboard.info"))
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: blue; font-size: 10px;")
        layout.addWidget(info_label)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line2)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.update_connection_status(False)
        layout.addWidget(self.status_label)

        layout.addStretch()

        self.qr_label = qr_label
        self.info_label = info_label

    def _apply_mode_ui(self) -> None:
        """根据当前模式更新菜单勾选状态和元素显隐。"""
        if self._switching:
            blank = QPixmap(250, 250)
            blank.fill(Qt.white)
            self.qr_label.setPixmap(blank)
            return

        if self._mode == TunnelMode.LAN:
            self.info_label.setVisible(True)
            self.cf_info_label.setVisible(False)
            self.switch_network_action.setEnabled(True)
            self._refresh_qr()
        else:
            self.info_label.setVisible(False)
            self.cf_info_label.setVisible(True)
            self.switch_network_action.setEnabled(False)
            if self._tunnel_url:
                self._refresh_qr()
            else:
                self._set_ip_text(self.i18n.tr("dashboard.cf_connecting"))

        # 审批通知在显示中时把说明标签再让回去：本方法会被模式切换/重连等路径调用，
        # 不补这一手就会让说明标签与审批面板同时占着同一块位置。
        if self._approval_frame.isVisible():
            self._set_main_info_visible(False)

    def _sync_menu_checks(self) -> None:
        """根据当前模式同步菜单勾选状态。"""
        self.act_lan.setChecked(self._mode == TunnelMode.LAN)
        self.act_cf.setChecked(self._mode == TunnelMode.CLOUDFLARE)
        # Cloudflare 模式下禁用 TOFU（公网无信任锚，强制扫码认证）
        self.act_auth_tofu.setEnabled(self._mode == TunnelMode.LAN)
        eff = effective_auth_method(self._auth_method, self._mode)
        self.act_auth_tofu.setChecked(eff == "tofu")
        self.act_auth_url_fragment.setChecked(eff == "url_fragment")

    def _set_busy(self, busy: bool) -> None:
        """进入/退出「切换中·重启中」状态：期间禁止再次触发网络菜单的操作。

        切换模式与重启服务共用一个状态位——一次网络动作没结束前再点任何一个，
        都只会让服务端白重启一次，必须挡住。
        """
        self._switching = busy
        self.act_lan.setEnabled(not busy)
        self.act_cf.setEnabled(not busy)
        self.act_restart_service.setEnabled(not busy)

    def _on_mode_clicked(self, target_mode: TunnelMode) -> None:
        """点击模式切换菜单项。"""
        if target_mode == self._mode:
            return
        if self._switching:
            self._sync_menu_checks()
            return
        self._set_busy(True)
        self._set_ip_text(self.i18n.tr("dashboard.switching"))
        self._mode = target_mode
        # 换模式＝旧的保活结论作废（保活只在 Cloudflare 模式运行）
        self._reset_tunnel_reachability()
        set_mode(target_mode)
        self._apply_mode_ui()
        if self._mode_switch_callback:
            self._mode_switch_callback(target_mode)

    def _on_auth_method_clicked(self, auth_method: str) -> None:
        """点击认证方式菜单项（"tofu" 手动审批 / "url_fragment" 扫码认证）。"""
        if auth_method == self._auth_method:
            return
        # Cloudflare 模式拒绝 TOFU，公网无信任锚
        if auth_method == "tofu" and self._mode == TunnelMode.CLOUDFLARE:
            self._sync_menu_checks()
            return
        self._auth_method = auth_method
        self.sm.set("auth_method", auth_method)
        if self._algorithm_change_callback:
            self._algorithm_change_callback(auth_method)
        self._refresh_qr()
        self.update_connection_status(self.connected)
        self._sync_menu_checks()

    def _on_input_mode_clicked(self, mode: str) -> None:
        """点击上屏方式菜单项（paste 剪贴板 / type 模拟键盘 / direct 直接输入），立即持久化。"""
        if mode != self.sm.get("text_input_mode", "paste"):
            self.sm.set("text_input_mode", mode)
        self._sync_input_checks()

    def _on_input_mode_setting_changed(self, _mode) -> None:
        """配置变更回调：偏好设置 / 托盘菜单改动后同步勾选状态。"""
        self._sync_input_checks()

    def _sync_input_checks(self) -> None:
        """根据当前配置同步上屏方式菜单的勾选状态。"""
        mode = self.sm.get("text_input_mode", "paste")
        self.act_input_paste.setChecked(mode == "paste")
        self.act_input_type.setChecked(mode == "type")
        self.act_input_direct.setChecked(mode == "direct")

    def on_switch_completed(self) -> None:
        """模式切换 / 重启服务结束（成功或失败），恢复菜单可用状态。"""
        self._set_busy(False)
        self._sync_menu_checks()
        self._apply_mode_ui()

    def _get_qr_url(self) -> str:
        """获取当前 QR 码 URL（含 PC 公钥 fragment）。"""
        if self._mode == TunnelMode.CLOUDFLARE and self._tunnel_url:
            url = self._tunnel_url
        else:
            url = f"http://{self._lan_ip}:{self._lan_port}"
        if self._secure_channel:
            url = self._secure_channel.append_to_url(url)
        return url
    def _set_ip_text(self, text: str) -> None:
        """更新地址栏文本。

        必须用 setPlainText：URL 里的 `&`、`<`、`>` 在 QTextBrowser 里会被
        当 HTML 解析，走 setText 会显示错乱。同时把整段设为居中，并把视图
        拉回开头 —— 否则长 URL 会默认停在末尾，看到的是尾巴。
        """
        self.ip_label.setPlainText(text)
        self.ip_label.setToolTip(text)          # 万一真被高度截断，悬停看全文
        self.ip_label.selectAll()
        self.ip_label.setAlignment(Qt.AlignCenter)
        self.ip_label.moveCursor(QTextCursor.Start)   # 清选择 + 滚回开头
    def _refresh_qr(self) -> None:
        """刷新 QR 码和地址栏。"""
        if self._switching:
            return
        url = self._get_qr_url()
        self.qr_label.setPixmap(make_qr_pixmap(url))
        self._set_ip_text(url)

    def update_tunnel_url(self, url: Optional[str]) -> None:
        """更新隧道 URL（Cloudflare 模式下更新二维码和地址）。

        收到有效 URL 意味着隧道已建立，自动结束切换状态。
        """
        self._tunnel_url = url
        if self._mode == TunnelMode.CLOUDFLARE:
            if url:
                # 新域名到手＝隧道刚重建，之前的「失效」标记随之作废
                self._reset_tunnel_reachability()
                if self._switching:
                    self.on_switch_completed()
                else:
                    self._refresh_qr()
            else:
                self._set_ip_text("Cloudflare: " + self.i18n.tr("dashboard.status_disconnected"))

    def get_mode(self) -> TunnelMode:
        """返回当前模式。"""
        return self._mode

    def _setup_menu(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)

        program_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_program"))
        network_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_network"))
        # 保留引用：菜单项顺序是功能语义的一部分（重启服务固定在末尾），
        # 单独存一下也避免只有局部引用时的生命周期问题
        self.network_menu = network_menu
        input_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_input_mode"))
        help_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_help"))

        # 偏好设置
        settings_action = QAction(self.i18n.tr("dashboard.menu_action"), self)
        settings_action.triggered.connect(self._open_settings)
        program_menu.addAction(settings_action)

        # 命令配置
        commands_action = QAction(self.i18n.tr("dashboard.menu_command"), self)
        commands_action.triggered.connect(self._open_commands_dialog)
        program_menu.addAction(commands_action)

        if not is_frozen():
            # 鼠标曲线调试（临时工具，验证帧率与位移是否抖动）
            debug_action = QAction("鼠标曲线调试", self)
            debug_action.triggered.connect(self._open_mouse_debug)
            program_menu.addAction(debug_action)

        # 分隔线 + 退出
        program_menu.addSeparator()
        exit_action = QAction(self.i18n.tr("dashboard.menu_exit"), self)
        exit_action.triggered.connect(self._quit_app)
        program_menu.addAction(exit_action)

        # 网络菜单 - 模式切换
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)

        self.act_lan = QAction(self.i18n.tr("dashboard.btn_lan"), self)
        self.act_lan.setCheckable(True)
        self.act_lan.setChecked(self._mode == TunnelMode.LAN)
        self.act_lan.triggered.connect(lambda: self._on_mode_clicked(TunnelMode.LAN))
        mode_group.addAction(self.act_lan)
        network_menu.addAction(self.act_lan)

        self.act_cf = QAction(self.i18n.tr("dashboard.btn_cloudflare"), self)
        self.act_cf.setCheckable(True)
        self.act_cf.setChecked(self._mode == TunnelMode.CLOUDFLARE)
        self.act_cf.triggered.connect(lambda: self._on_mode_clicked(TunnelMode.CLOUDFLARE))
        mode_group.addAction(self.act_cf)
        network_menu.addAction(self.act_cf)

        network_menu.addSeparator()

        # 认证方式：TOFU（手动审批）或 URL fragment（扫码认证）
        # 加密永远开启，此处仅选择认证方式；CF 模式强制 url_fragment
        auth_group = QActionGroup(self)
        auth_group.setExclusive(True)

        self.act_auth_tofu = QAction(self.i18n.tr("dashboard.auth_tofu"), self)
        self.act_auth_tofu.setCheckable(True)
        self.act_auth_tofu.triggered.connect(lambda: self._on_auth_method_clicked("tofu"))
        auth_group.addAction(self.act_auth_tofu)
        network_menu.addAction(self.act_auth_tofu)

        self.act_auth_url_fragment = QAction(self.i18n.tr("dashboard.auth_url_fragment"), self)
        self.act_auth_url_fragment.setCheckable(True)
        self.act_auth_url_fragment.triggered.connect(lambda: self._on_auth_method_clicked("url_fragment"))
        auth_group.addAction(self.act_auth_url_fragment)
        network_menu.addAction(self.act_auth_url_fragment)

        network_menu.addSeparator()

        # 切换网络地址（仅局域网模式可用）
        self.switch_network_action = QAction(self.i18n.tr("dashboard.menu_switch_network"), self)
        self.switch_network_action.triggered.connect(self._on_switch_network)
        self.switch_network_action.setEnabled(self._mode == TunnelMode.LAN)
        network_menu.addAction(self.switch_network_action)

        network_menu.addSeparator()

        # 重启服务（两种模式都可用）：按当前模式把服务重来一遍——省掉用户
        # 「先切到局域网、再切回 Cloudflare」那套操作（切到已选中的模式是空操作）。
        # 重启会换新身份（新的 secret 路径与密钥对 → 新二维码），手机端需重新配对：
        # 扫码认证重新扫二维码，手动审批则再点一次「允许」；CF 模式还会换上新的
        # 临时域名，是隧道被回收后的自救路径。
        self.act_restart_service = QAction(self.i18n.tr("dashboard.menu_restart_service"), self)
        self.act_restart_service.triggered.connect(self._on_restart_service)
        network_menu.addAction(self.act_restart_service)

        # 上屏方式菜单 - 与偏好设置面板中的「上屏方式」等价，作为快速配置入口。
        # 与「网络」菜单保持一致：用互斥组，Qt 会画成单选圆点。
        input_group = QActionGroup(self)
        input_group.setExclusive(True)

        self.act_input_paste = QAction(self.i18n.tr("dashboard.input_clipboard"), self)
        self.act_input_paste.setCheckable(True)
        self.act_input_paste.triggered.connect(lambda: self._on_input_mode_clicked("paste"))
        input_group.addAction(self.act_input_paste)
        input_menu.addAction(self.act_input_paste)

        self.act_input_type = QAction(self.i18n.tr("dashboard.input_type"), self)
        self.act_input_type.setCheckable(True)
        self.act_input_type.triggered.connect(lambda: self._on_input_mode_clicked("type"))
        input_group.addAction(self.act_input_type)
        input_menu.addAction(self.act_input_type)

        self.act_input_direct = QAction(self.i18n.tr("dashboard.input_direct"), self)
        self.act_input_direct.setCheckable(True)
        self.act_input_direct.triggered.connect(lambda: self._on_input_mode_clicked("direct"))
        self.act_input_direct.setToolTip(self.i18n.tr("settings.input_mode_tooltip"))
        input_group.addAction(self.act_input_direct)
        input_menu.addAction(self.act_input_direct)

        # 初始化勾选状态（包括 CF 模式下 none 强制变为加密的显示）
        self._sync_menu_checks()
        self._sync_input_checks()

        # 帮助菜单
        help_action = QAction(self.i18n.tr("dashboard.menu_help_guide"), self)
        help_action.triggered.connect(self.open_user_guide)
        help_menu.addAction(help_action)

        about_action = QAction(self.i18n.tr("dashboard.menu_about"), self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    def _open_commands_dialog(self):
        dlg = CommandsDialog(self)
        dlg.exec_()

    def _on_switch_network(self):
        """触发切换网络回调"""
        if self._restart_network_callback:
            self._restart_network_callback()

    def _on_restart_service(self) -> None:
        """点击「重启服务」：按当前模式把服务重来一遍。

        会换新身份（secret 路径与密钥对都变），手机端需重新配对：扫码认证重新扫
        二维码，手动审批则再点一次「允许」。
        这里先清掉 `_tunnel_url` 并擦掉二维码：否则 CF 重启失败时，界面会把
        已经作废的旧域名当成有效地址继续显示，用户扫了也连不上。
        """
        if self._switching:
            return
        self._set_busy(True)
        self._set_ip_text(self.i18n.tr("dashboard.restarting"))
        self._tunnel_url = None
        # 重启正是失效时的自救动作，清掉失效标记（拿到新 URL 后会再次刷新）
        self._reset_tunnel_reachability()
        self._apply_mode_ui()  # _switching=True → 先清空二维码，避免扫到已作废的旧码
        if self._restart_service_callback:
            self._restart_service_callback()

    def update_network(self, ip: str, port: int):
        """更新主界面的 IP 和二维码显示"""
        self._lan_ip = ip
        self._lan_port = port
        self._refresh_qr()

    def show_about(self):
        version, commit, _ = get_build_info()
        content = self.i18n.tr("about.content", version=version, commit=commit)
        QMessageBox.about(self, self.i18n.tr("about.title"), content)

    def open_user_guide(self):
        guide_path = get_app_root() / "USER_GUIDE.md"
        if guide_path.exists():
            if sys.platform == "win32":
                subprocess.Popen(["notepad.exe", str(guide_path)],
                                 shell=False,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.run(["open", str(guide_path)] if sys.platform == "darwin" else ["xdg-open", str(guide_path)])
        else:
            QMessageBox.warning(self, self.i18n.tr("help.warning"),
                                self.i18n.tr("help.file_not_found"))

    def _algo_display_name(self, algo: str) -> str:
        """算法显示名：优先取 locale，缺失时回退为原始算法名。"""
        key = f"dashboard.algo_{algo}"
        tr = self.i18n.tr(key)
        return tr if tr != key else algo

    def update_connection_status(self, connected: bool, algorithm: Optional[str] = None) -> None:
        self.connected = connected
        # algorithm 仅在 connect 事件中携带；断开时清空协商结果
        if connected and algorithm is not None:
            self._negotiated_algo = algorithm
        elif not connected:
            self._negotiated_algo = None
        if connected:
            text = '<span style="color:green;">●</span> ' + self.i18n.tr("dashboard.status_connected")
            # 加密永远开启，状态栏展示认证方式 + 协商算法
            eff_auth = effective_auth_method(self._auth_method, self._mode)
            auth_display = self.i18n.tr("dashboard.status_auth_tofu" if eff_auth == "tofu" else "dashboard.status_auth_url_fragment")
            text += ' <span style="color:#666;">| ' + auth_display
            if self._negotiated_algo and self._negotiated_algo != "none":
                algo_display = self._algo_display_name(self._negotiated_algo)
                text += ' / ' + algo_display
            text += '</span>'
        else:
            text = '<span style="color:red;">●</span> ' + self.i18n.tr("dashboard.status_disconnected")

        # 末段：隧道失效（保活探测连续失败）——只有 Cloudflare 模式才有保活，
        # 局域网模式不受 Ready 影响
        if self._mode == TunnelMode.CLOUDFLARE and not self._tunnel_reachable:
            text += ' <span style="color:#c62828;">| ' + self.i18n.tr("dashboard.tunnel_lost") + '</span>'
        self.status_label.setText(text)

    def set_tunnel_reachability(self, reachable: bool) -> None:
        """隧道公网入口可达性翻转（TunnelKeepalive 探测结果）。

        用状态栏而不是托盘通知——理由是**可见性**，不是「打扰」：失效只会在用户
        不在电脑前时发生，因为人正常使用时保活流量是持续不断的，隧道闲不到被回收
        的条件。而托盘通知只停留几秒，用户不在场等于必然错过，回来时屏幕上没有
        任何痕迹，根本不知道要重扫码。状态栏是常驻的，用户回来一眼就能看到
        「隧道失效，请重启服务」——这才是闭环成立的前提。
        顺带的好处是不抢焦点（60s 一轮，偶发抖动走通知会变成打扰）。
        （发布版没有任何日志出口，这里是唯一可视的落点。）
        """
        if reachable == self._tunnel_reachable:
            return
        self._tunnel_reachable = reachable
        # 复用现有入口重绘整行，避免与加密状态段各自拼一半
        self.update_connection_status(self.connected)

    def _reset_tunnel_reachability(self) -> None:
        """清掉失效标记并重绘状态栏（新域名到手 / 切换模式 / 重启服务时）。

        必须显式重绘：状态栏是自拼的字符串，只改内部状态不会自动刷新，用户会
        继续看到已经过期的「隧道失效」提示——刚做完重启就还挂着这句话最误导人。
        """
        if self._tunnel_reachable:
            return
        self._tunnel_reachable = True
        self.update_connection_status(self.connected)

    def show_hide_on_tray_message(self):
        if getattr(self, '_already_show_hide_on_tray_message', False):
            return
        self._already_show_hide_on_tray_message = True
        self.tray.show_message(
            self.i18n.tr("tray.minimized_title"),
            self.i18n.tr("tray.minimized_message"),
            timeout=3000
        )

    def _quit_app(self):
        """从菜单点击退出，直接终止程序"""
        self._is_force_quitting = True
        QApplication.quit()

    def closeEvent(self, event):
        """重写关闭事件：根据配置决定行为。若为菜单强制退出则直接放行。"""
        # 如果是菜单触发的强制退出，直接放行，不做任何花活
        if getattr(self, '_is_force_quitting', False):
            event.accept()
            return

        close_action = self.sm.get("close_action", None)

        if close_action == "quit":
            event.accept()
            return
        event.ignore()
        if close_action == "tray":
            self.hide()
            self.show_hide_on_tray_message()
        else:
            self._show_close_choice_dialog()

    def _show_close_choice_dialog(self):
        """显示关闭行为选择对话框（首次使用）"""
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.tr("close_choice.title"))
        dialog.setModal(True)
        dialog.setMinimumWidth(350)

        layout = QVBoxLayout(dialog)

        label = QLabel(self.i18n.tr("close_choice.prompt"))
        label.setWordWrap(True)
        layout.addWidget(label)

        quit_radio = QRadioButton(self.i18n.tr("close_choice.quit"))
        tray_radio = QRadioButton(self.i18n.tr("close_choice.tray"))
        quit_radio.setChecked(True)
        layout.addWidget(quit_radio)
        layout.addWidget(tray_radio)

        remember_check = QCheckBox(self.i18n.tr("close_choice.remember"))
        layout.addWidget(remember_check)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        if dialog.exec() == QDialog.Accepted:
            action = "quit" if quit_radio.isChecked() else "tray"
            if remember_check.isChecked():
                self.sm.set("close_action", action)
            if action == "quit":
                QApplication.quit()
            else:
                self.hide()
                self.show_hide_on_tray_message()
