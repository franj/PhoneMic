"""
关于窗口。

为什么不用 ``QMessageBox.about``：它只接受一段富文本，链接能不能点取决于该平台
QLabel 的交互设置，版本号也没法做成「可选中复制的小字」——而用户报问题时最想给
的恰好就是版本号和 commit。这里固定宽度自己排一次版：图标 + 名称 + 版本 + 一句话
简介 + 项目主页 + 「关闭」。

链接用 QLabel 的富文本承载（``openExternalLinks=True``，点击交给系统默认浏览器），
不用 QPushButton 伪装——按钮点下去没有「这是链接」的暗示，鼠标也不会变成手型。
"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout,
)

from phonemic.utils.i18n import I18n
from phonemic.utils.paths import get_build_info

# 关于窗口底部那个链接。换域名时改这两行即可（文字与 href 分开，是为了不把
# scheme 显示出来——「https://」对用户没有信息量）。
PROJECT_URL = "https://github.com/franj/PhoneMic"
PROJECT_URL_TEXT = "github.com/franj/PhoneMic"


class AboutDialog(QDialog):
    """关于窗口（模态，固定宽度，内容决定高度）。"""

    ICON_SIZE = 56

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.i18n = I18n.instance()
        self.setWindowTitle(self.i18n.tr("about.title"))
        self.setModal(True)
        self.setSizeGripEnabled(False)
        self.setFixedWidth(460)
        self._setup_ui()

    # ---- 内部 ----

    def _icon_label(self) -> QLabel:
        """标题左侧的程序图标。取不到图标时留空，不画占位框。"""
        label = QLabel()
        label.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)

        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.windowIcon()
        if icon.isNull():
            return label

        size = QSize(self.ICON_SIZE, self.ICON_SIZE)
        label.setPixmap(icon.pixmap(size, self.devicePixelRatioF()))
        return label

    def _secondary(self, label: QLabel, delta: float = 1.0) -> None:
        """次要文字：小一号 + 主题感知的灰。

        颜色取自调色板而不是写死 ``color: gray``：深色主题下灰色要更亮才读得清，
        写死一份颜色等于替未来某个主题做决定。
        """
        font = label.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(7.5, font.pointSizeF() - delta))
        else:
            font.setPixelSize(max(10, font.pixelSize() - 1))
        label.setFont(font)

        palette = label.palette()
        color = palette.color(QPalette.PlaceholderText)
        if not color.isValid() or color.alpha() == 0:
            color = palette.color(QPalette.Disabled, QPalette.WindowText)
        palette.setColor(QPalette.WindowText, color)
        label.setPalette(palette)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 20)
        root.setSpacing(0)

        # ---- 图标 + 名称 / 版本 ----
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(self._icon_label(), 0, Qt.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(3)

        name = QLabel("PhoneMic")
        name_font = name.font()
        if name_font.pointSizeF() > 0:
            name_font.setPointSizeF(name_font.pointSizeF() + 6)
        else:
            name_font.setPixelSize(22)
        name_font.setBold(True)
        name.setFont(name_font)
        titles.addWidget(name)

        version, commit, _ = get_build_info()
        self.version_label = QLabel(
            self.i18n.tr("about.version_line", version=version, commit=commit)
        )
        # 可选中：用户报问题时能直接复制这一行，不用手抄 commit
        self.version_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.version_label.setToolTip(self.i18n.tr("about.version_tooltip"))
        self._secondary(self.version_label)
        titles.addWidget(self.version_label)

        titles.addStretch(1)
        head.addLayout(titles, 1)
        root.addLayout(head)

        # ---- 一句话简介 ----
        root.addSpacing(18)
        tagline = QLabel(self.i18n.tr("about.tagline"))
        tagline.setWordWrap(True)
        root.addWidget(tagline)

        # ---- 分隔线 + 技术栈 / 版权 ----
        root.addSpacing(18)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        root.addWidget(line)
        root.addSpacing(12)

        tech = QLabel(self.i18n.tr("about.tech_stack"))
        self._secondary(tech)
        root.addWidget(tech)

        root.addSpacing(4)
        copyright_label = QLabel(self.i18n.tr("about.copyright"))
        self._secondary(copyright_label)
        root.addWidget(copyright_label)

        # ---- 项目主页 + 关闭 ----
        root.addSpacing(20)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.link_label = QLabel(
            f'<a href="{PROJECT_URL}">{PROJECT_URL_TEXT} ↗</a>'
        )
        self.link_label.setOpenExternalLinks(True)
        self.link_label.setToolTip(PROJECT_URL)
        bottom.addWidget(self.link_label)
        bottom.addStretch(1)

        self.close_button = QPushButton(self.i18n.tr("about.close"))
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.close_button)

        root.addLayout(bottom)
