# gui/hud.py
import sys
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QApplication, QSizePolicy
from PySide6.QtGui import QCursor, QGuiApplication, QFont, QFontDatabase

from phonemic.utils.settings_manager import SettingsManager


class HudSignals(QObject):
    preview_text = Signal(str)


class HudWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.sm = SettingsManager.instance()

        # ----- 窗口属性 -----
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        # ----- 标签样式（不再内嵌 font-size，由 setFont 控制）-----
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(400)
        self.label.setMinimumWidth(50)
        self.label.setStyleSheet("""
            QLabel {
                color: white;
                background-color: rgba(0, 0, 0, 180);
                border-radius: 12px;
                padding: 12px 16px;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
            }
        """)
        self.label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Minimum
        )

        # ----- 布局 -----
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self.setLayout(layout)

        # ----- 定时器（超时值从配置读取）-----
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

        # 加载初始配置并应用
        self._load_initial_config()
        self._setup_hot_reload()

        # 预热状态（见 prewarm）
        self._prewarmed = False
        self._prewarming = False

        self.hide()  # 初始隐藏

    def _load_initial_config(self):
        """加载初始配置：超时时间、字体"""
        timeout_sec = self.sm.get("hud_timeout_sec", 5)
        self.timeout_ms = timeout_sec * 1000

        font_val = self.sm.get("hud_font_size", 14)
        self._apply_font(font_val)

    def _setup_hot_reload(self):
        """连接配置变更信号，实现热重载"""
        self.sm.connect_changed("hud_timeout_sec", self._update_timeout)
        self.sm.connect_changed("hud_font_size", self._update_font)

    def _update_timeout(self, seconds: int):
        """热重载：更新超时时间（秒）"""
        self.timeout_ms = seconds * 1000
        # 如果当前定时器正在运行，动态修改剩余时间（可选，简单处理：重启定时器）
        if self.timer.isActive():
            # 重启定时器会使计时从头开始，更符合预期
            self.timer.start(self.timeout_ms)

    def _apply_font(self, font_value):
        """根据配置值创建并应用字体到 label"""
        font = QFont()
        if font_value == "system":
            # 获取系统默认字体大小
            sys_font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
            point_size = sys_font.pointSize()
            if point_size <= 0:
                point_size = QApplication.font().pointSize()
            if point_size <= 0:
                point_size = 14
            font.setPointSize(point_size)
        elif isinstance(font_value, int):
            font.setPointSize(font_value)
        else:
            # 兼容字符串数字
            try:
                font.setPointSize(int(font_value))
            except:
                font.setPointSize(14)
        # 保留原有字体族（从样式表获取）
        font.setFamily("Microsoft YaHei, Segoe UI, sans-serif")
        self.label.setFont(font)

        # 如果当前窗口可见，需要重新计算尺寸以适应新字体
        if self.isVisible():
            current_text = self.label.text()
            if current_text:
                # 重新触发预览更新（复用原有逻辑）
                self.on_preview_text(current_text)

    def _update_font(self, font_value):
        """热重载：更新字体大小"""
        self._apply_font(font_value)

    def prewarm(self):
        """预热首次显示所需的一次性初始化，避免第一次说话时窗口要等半秒才出现。

        实测首次 show() 需要 300~600ms（Qt 的 backing store、绘制引擎、字体引擎、
        DWM 合成通道等一次性开销），之后只要几毫秒。这部分开销与窗口是否可见无关，
        只与"有没有真正 show 过"有关——所以直接用 WA_DontShowOnScreen 走一遍显示
        流程即可，窗口全程不会被映射到屏幕上。相比"移到屏幕外再 show"，这里不依赖
        坐标躲让，多屏 / 缩放 / 远程桌面下都不会意外露出来。

        代价是启动阶段多几百毫秒的一次性计算。这段计算无法拆分（Qt 的一次性同步
        初始化），所以只能由调用方延后触发、避开启动最忙的那一段——是"延后"，
        不是"利用空闲"。
        """
        if self._prewarmed or self._prewarming or self.isVisible():
            return
        self._prewarming = True
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.show()
        # 用 0 延时排到下一轮事件循环，避免在本次调用里嵌套处理事件（可能重入）
        QTimer.singleShot(0, self._end_prewarm)

    def _end_prewarm(self):
        """结束预热：先隐藏、再撤掉"不上屏"属性。

        顺序不能反。预热期间 Qt 已经把窗口当成"已显示"（WA_Mapped / WA_WState_Hidden
        都被置成已上屏），此时只清 WA_DontShowOnScreen 并不会让原生窗口真正 map，
        后续 show() 会被当成空操作——widget.isVisible() 是 True，屏幕上却什么都没有
        （实测 QWindow.isVisible()=False、isExposed()=False）。先 hide() 把"已显示"
        状态清掉，再让调用方后续的 show() 重新走一遍 map，才能真的上屏。
        """
        if not self._prewarming:
            return
        self._prewarming = False
        self._prewarmed = True
        self.hide()
        self.setAttribute(Qt.WA_DontShowOnScreen, False)

    def on_preview_text(self, text: str):
        # 真实内容在预热期间到达时要确保状态干净：这里会隐藏窗口，紧接着下面的
        # show() 重新 map，内容正常上屏。预热早已结束则原样返回，无副作用。
        self._end_prewarm()

        if text == "":
            self.timer.stop()
            self.hide()
            return

        self.label.setText(text)

        # ----- 手动计算窗口尺寸（依赖当前 label 的字体和样式表）-----
        left_right_padding = 32   # 16+16
        top_bottom_padding = 24   # 12+12

        font_metrics = self.label.fontMetrics()
        max_width = 400
        rect = font_metrics.boundingRect(
            0, 0, max_width - left_right_padding, 0,
            Qt.TextWordWrap, text
        )
        text_width = rect.width()
        text_height = rect.height()

        label_width = min(text_width + left_right_padding, max_width)
        label_height = text_height + top_bottom_padding

        self.label.setFixedSize(label_width, label_height)
        self.resize(label_width, label_height)

        self._reposition()
        self.show()
        # 真实显示过一次，一次性初始化已经花掉了，预热不再需要（也避免对已上屏
        # 的窗口设置 WA_DontShowOnScreen）。
        self._prewarmed = True
        self.timer.start(self.timeout_ms)  # 使用动态超时值

    def _reposition(self):
        """将窗口置于鼠标光标所在屏幕的底部中央（距离底部20px）"""
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen is None:
            screen = self.screen()
            if screen is None:
                screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + available.height() - self.height() - 20
        self.move(x, y)

    def showEvent(self, event):
        """每次显示时重新定位（以防屏幕变更）"""
        self._reposition()
        super().showEvent(event)


# 全局信号单例
_hud_signals = None

def get_hud_signals() -> HudSignals:
    global _hud_signals
    if _hud_signals is None:
        _hud_signals = HudSignals()
    return _hud_signals


# 独立测试入口
if __name__ == "__main__":
    app = QApplication(sys.argv)
    hud = HudWindow()
    signals = get_hud_signals()
    signals.preview_text.connect(hud.on_preview_text)

    from PySide6.QtCore import QTimer
    test_messages = [
        "Hello",
        "Hello World",
        "这是一段较长的中文测试文本，用来验证自动换行和宽度限制功能。",
        "",
        "最后一条"
    ]
    idx = 0
    def send():
        if idx < len(test_messages):
            msg = test_messages[idx]
            print(f"发送: '{msg}'")
            signals.preview_text.emit(msg)
            idx += 1
            QTimer.singleShot(2000, send)
        else:
            print("测试结束")
    QTimer.singleShot(1000, send)

    sys.exit(app.exec())