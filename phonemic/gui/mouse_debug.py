"""鼠标帧调试监视器（临时工具，验证帧率与位移是否抖动）。

由 PC 端在 perform_mouse 的 move 分支把 (dx, dy) 喂进来，本组件按到达时间
算出瞬时帧率与每帧位移，画成两条时间序列（横轴时间、竖轴数值）。

独立成弹出窗口（MouseDebugWindow）：默认隐藏，从 Dashboard「程序」菜单打开；
隐藏时不重绘（paintEvent 不开销），关闭（点 X）仅隐藏不销毁，历史采样保留。

线程：push() 与绘制都在 Qt 主线程（on_backend_event 在主线程消费），无需加锁。
"""
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import QSizePolicy, QWidget, QVBoxLayout

# 窗口保留最近 4 秒的样本（60Hz 下 240 个，120Hz 下 480 个）
MAX_SAMPLES = 480
WINDOW_SEC = 4.0


class MouseDebugWidget(QWidget):
    """上下两条时间序列：上=帧率 Hz，下=每帧位移 px。"""

    BG = QColor("#ffffff")
    GRID = QColor("#ececec")
    AXIS = QColor("#c8c8c8")
    TEXT = QColor("#666666")
    HZ_LINE = QColor("#1D9E75")
    HZ_REF = QColor("#9FE1CB")
    PX_LINE = QColor("#378ADD")
    WARN = QColor("#E24B4A")

    def __init__(self, parent=None):
        super().__init__(parent)
        # (到达时刻, 瞬时帧率, 每帧位移px)
        self._samples = deque(maxlen=MAX_SAMPLES)
        self._last_t = None
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._timer = QTimer(self)
        self._timer.setInterval(100)   # 10fps 重绘即可，避免自己制造 jank
        self._timer.timeout.connect(self.update)
        # 隐藏时不启动定时器，避免空耗 CPU；由 showEvent 启动
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event):  # noqa: N802
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    # ---------- 数据入口 ----------

    def push(self, dx: int, dy: int) -> None:
        """perform_mouse 每执行一个 move 帧调用一次。"""
        now = time.perf_counter()
        dist = (dx * dx + dy * dy) ** 0.5
        if self._last_t is not None:
            dt = now - self._last_t
            if 0 < dt < 0.5:      # 丢弃"停了一阵又重新开始"的超大间隔
                self._samples.append((now, 1.0 / dt, dist))
        self._last_t = now

    def reset(self) -> None:
        self._samples.clear()
        self._last_t = None

    # ---------- 绘制 ----------

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, self.BG)

        if not self._samples:
            p.setPen(QPen(self.TEXT))
            p.drawText(0, 0, w, h, Qt.AlignCenter, "等待鼠标帧…（推动手机端摇杆）")
            return

        # 只画时间窗内的样本
        t_end = self._samples[-1][0]
        t_start = t_end - WINDOW_SEC
        pts = [s for s in self._samples if s[0] >= t_start]
        if len(pts) < 2:
            p.setPen(QPen(self.TEXT))
            p.drawText(0, 0, w, h, Qt.AlignCenter, "等待鼠标帧…（推动手机端摇杆）")
            return

        hzs = [s[1] for s in pts]
        pxs = [s[2] for s in pts]

        # 两图各占一半高度，顶部留一行统计文字
        head = 16
        plot_h = (h - head - 8) // 2
        top_rect = (0, head, w, plot_h)
        bot_rect = (0, head + plot_h + 8, w, plot_h)

        p.setPen(QPen(self.TEXT))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(4, 0, w - 8, head, Qt.AlignVCenter | Qt.AlignLeft,
                   self._fmt_stats(hzs, pxs))

        self._draw_series(p, top_rect, pts, 1, "帧率 Hz", self.HZ_LINE,
                          y_max=max(90.0, min(400.0, max(hzs) * 1.15)),
                          ref=60.0, ref_color=self.HZ_REF)
        self._draw_series(p, bot_rect, pts, 2, "每帧位移 px", self.PX_LINE,
                          y_max=max(4.0, max(pxs) * 1.15))

    def _draw_series(self, p, rect, pts, idx, title, color, y_max,
                     ref=None, ref_color=None):
        x, y, w, h = rect
        p.setPen(QPen(self.AXIS))
        p.setBrush(Qt.NoBrush)
        p.drawRect(x, y, w - 1, h - 1)

        t_end = pts[-1][0]
        t_start = pts[0][0]
        span = max(1e-6, t_end - t_start)

        def to_px(i, v):
            px = x + (pts[i][0] - t_start) / span * (w - 2) + 1
            py = y + h - 1 - (min(v, y_max) / y_max) * (h - 2)
            return px, py

        p.setPen(QPen(self.GRID))
        for k in range(1, 4):
            gy = y + h - 1 - k * (h - 2) / 4
            p.drawLine(x + 1, gy, x + w - 2, gy)

        if ref is not None and ref <= y_max:
            ry = y + h - 1 - (ref / y_max) * (h - 2)
            p.setPen(QPen(ref_color, 1, Qt.DashLine))
            p.drawLine(x + 1, ry, x + w - 2, ry)

        pen = QPen(color, 1.6)
        p.setPen(pen)
        prev = None
        for i, s in enumerate(pts):
            cur = to_px(i, s[idx])
            if prev is not None:
                p.drawLine(prev[0], prev[1], cur[0], cur[1])
            prev = cur

        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(QPen(self.TEXT))
        p.drawText(x + 4, y + 2, w - 8, 14, Qt.AlignVCenter | Qt.AlignLeft,
                   f"{title}  上限 {y_max:.0f}")
        p.drawText(x + 4, y + h - 16, w - 8, 14,
                   Qt.AlignVCenter | Qt.AlignRight, f"{span:.1f}s 窗口")

    @staticmethod
    def _fmt_stats(hzs, pxs) -> str:
        def med(v):
            s = sorted(v)
            return s[len(s) // 2]

        def pct(v, q):
            s = sorted(v)
            return s[min(len(s) - 1, int(len(s) * q))]

        n = len(hzs)
        mean = sum(hzs) / n
        var = sum((v - mean) ** 2 for v in hzs) / n
        sd = var ** 0.5
        px_med, px_max = med(pxs), max(pxs)
        return (f"帧率 中位 {med(hzs):.1f}  p95 {pct(hzs, 0.95):.1f}  "
                f"最大 {max(hzs):.1f}  标准差 {sd:.2f}  (n={n})    "
                f"每帧位移 中位 {px_med:.1f}  最大 {px_max:.1f}")


class MouseDebugWindow(QWidget):
    """独立调试窗口（临时）。默认隐藏，从 Dashboard「程序」菜单打开。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("鼠标曲线调试")
        self.setWindowFlags(Qt.Window)
        self.widget = MouseDebugWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.widget)
        self.resize(460, 220)

    def closeEvent(self, event):  # noqa: N802
        # 点 X 仅隐藏不销毁，历史采样保留，从菜单可再次打开
        event.ignore()
        self.hide()
