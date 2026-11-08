"""
使用帮助窗口：左侧标题导航 + 右侧 Markdown 正文。

为什么不再把 USER_GUIDE.md 丢给记事本：那是 Markdown，记事本只负责把字节摊开，
`##`、`**`、表格的竖线全都原样露着。用户拿它当说明书读，读到的全是排版噪音。

渲染路线是 markdown-it-py 转 HTML、再交给 QTextBrowser.setHtml：

- 不用 ``QTextBrowser.setMarkdown``：它认标题级别但不给字号差（实测 h1~h4 和正文
  一样大），而「一眼看出章节层次」正是这个窗口存在的理由；它也不生成锚点，文档里
  现成的 ``[2.7 加密与认证](#27-加密与认证)`` 这类跳转会全部失效。
- 不用 ``QWebEngineView``：排版最省心，但安装包要背上 200MB+ 和
  QtWebEngineProcess.exe。为一份说明书不值。

目录不写死：每次打开都从渲染结果里的标题现扫（h1~h3），文档增删章节时程序零改动。
目录上方有个搜索框：输入即过滤目录（找章节），回车在正文里逐处跳转并高亮（找内容）。
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple
from urllib.parse import unquote

from markdown_it import MarkdownIt
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QKeySequence, QShortcut, QTextBlock, QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QSplitter,
    QTextBrowser, QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from phonemic.utils.i18n import I18n
from phonemic.utils.paths import get_app_root

logger = logging.getLogger(__name__)

GUIDE_BASENAME = "USER_GUIDE"

# 中文是原始版本，文件名不带语言后缀——这个文件名在打包脚本和 README 里都写死了，
# 不值得为了对称去改。其它语言是 `USER_GUIDE_<lang>.md`。
GUIDE_DEFAULT_LANG = "zh_CN"

# 目录只到 h3：h4 在这份文档里只有「（1）模拟按键」「（2）运行程序」两条，
# 收进目录只是把树撑长，阅读时更碍事。
TOC_MAX_LEVEL = 3

# Qt 的富文本引擎只认 CSS 2.1 的一个子集：元素选择器、font-*、color、
# background-color、margin/padding、line-height 可用；flex/grid/伪类通通无效。
# 所以这里只用元素选择器，别再往下加花活。
_STYLE = """
h1 { font-size: 19pt; font-weight: 600; color: #1b1b1b;
     margin-top: 2px; margin-bottom: 14px; }
h2 { font-size: 15pt; font-weight: 600; color: #1b1b1b;
     margin-top: 22px; margin-bottom: 8px; }
h3 { font-size: 12pt; font-weight: 600; color: #2b2b2b;
     margin-top: 16px; margin-bottom: 6px; }
h4 { font-size: 10pt; font-weight: 600; color: #2b2b2b;
     margin-top: 12px; margin-bottom: 4px; }
p  { line-height: 158%; margin-top: 4px; margin-bottom: 8px; }
li { line-height: 152%; margin-bottom: 3px; }
pre { background-color: #f7f7f7; padding: 8px; margin-top: 8px; margin-bottom: 10px; }
code { font-family: "Consolas", "Courier New", monospace;
       background-color: #f2f2f2; color: #a03030; }
pre code { background-color: #f7f7f7; color: #333333; }
a  { color: #1565c0; }
blockquote { background-color: #fafafa; color: #555555;
             margin-left: 8px; padding-left: 10px; }
th { background-color: #f3f3f3; font-weight: 600; }
td, th { padding: 5px; }
hr { color: #e2e2e2; }
"""


class Heading(NamedTuple):
    """文档里的一个标题。slug 是它的锚点名（见 slugify）。"""

    level: int
    text: str
    slug: str


def slugify(text: str) -> str:
    """按 GitHub 的规则把标题文本转成锚点。

    规则只要覆盖这份文档实际用到的形态：转小写、丢掉标点（编号里的点、全角括号都算
    标点）、空格换 ``-``。中文属于字母类别（Lo），整段保留——于是
    「2.7 加密与认证」得到 ``27-加密与认证``，正好和文档里已经写好的链接对得上，
    改版时不用同步改文档。
    """
    out: List[str] = []
    for ch in text.strip().lower():
        if ch in (" ", "-"):
            out.append("-")
        elif ch.isalnum() or unicodedata.category(ch)[0] in ("L", "M", "N"):
            out.append(ch)
    return "".join(out)


def _unique_slug(text: str, seen: Dict[str, int]) -> str:
    """同名标题依次加 ``-1``、``-2``，和 GitHub 的去重方式一致。"""
    base = slugify(text)
    if base in seen:
        seen[base] += 1
        return f"{base}-{seen[base]}"
    seen[base] = 0
    return base


def _markdown() -> MarkdownIt:
    """CommonMark + 表格 + 删除线。

    ``html=False`` 是有意的：说明书里不需要内嵌 HTML，关掉它就不会出现
    「文档里一段 raw HTML 把整页样式带跑偏」这种问题。
    """
    md = MarkdownIt("commonmark", {"html": False})
    md.enable(["table", "strikethrough"])
    return md


def build_html(markdown_text: str) -> Tuple[str, List[Heading]]:
    """把 Markdown 渲染成可交给 QTextBrowser 的 HTML，同时返回标题清单。

    标题清单和 HTML 出自同一次 parse，所以锚点和目录天然一致——不用二次解析文本，
    也就不会出现「目录里有、正文里点不到」这种对不上的情况。
    """
    md = _markdown()
    tokens = md.parse(markdown_text)

    seen: Dict[str, int] = {}
    headings: List[Heading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        text = tokens[index + 1].content if index + 1 < len(tokens) else ""
        slug = _unique_slug(text, seen)
        token.attrSet("id", slug)
        headings.append(Heading(int(token.tag[1:]), text, slug))

    body = md.renderer.render(tokens, md.options, {})

    # markdown-it 输出的 <table> 不带边框属性，Qt 画出来是一张没有线的表；
    # 表格在这份文档里是「三种上屏方式对比」这类内容的主要承载，必须有线。
    body = body.replace(
        "<table>", '<table border="1" cellspacing="0" cellpadding="6">'
    )

    html = f"<html><head><style>{_STYLE}</style></head><body>{body}</body></html>"
    return html, headings


def guide_path(lang: str) -> Path:
    """某语言的说明书路径。没有对应译文时回退到中文原版。"""
    root = get_app_root()
    if lang and lang != GUIDE_DEFAULT_LANG:
        candidate = root / f"{GUIDE_BASENAME}_{lang}.md"
        if candidate.exists():
            return candidate
    return root / f"{GUIDE_BASENAME}.md"


class HelpWindow(QDialog):
    """使用帮助窗口（非模态、可复用、独立顶层窗口）。

    非模态是刻意的：用户看说明书时通常正在操作主界面（照着步骤点按钮），模态窗口
    会把要参照的界面整个盖住。窗口对象由调用方持有，关掉只是隐藏，再次打开不用
    重新解析文档。

    **不接收 parent**，这是刻意的。Windows 上「有父窗口的对话框」是父窗口的 owned
    window，随父窗口一起收放、永远压在父窗口上面：用户切回主界面时说明书还盖着，
    想照着操作反而得先把它挪开或关掉，和「非模态方便对照」的初衷正好相反。
    独立成顶层窗口后它就是一扇普通窗口——点主界面它会让路，需要时点一下它又回来，
    用户按自己习惯的窗口习惯用就行。副作用是主界面收进托盘后它不会跟着消失，
    所以 Dashboard 在 hideEvent 里显式把它一起收起来（见 dashboard.py）。
    """

    DEFAULT_SIZE = (1000, 720)

    def __init__(self) -> None:
        super().__init__()
        self.i18n = I18n.instance()

        # 独立窗口要像个正常窗口：能最小化、能最大化、能 Alt+Tab 叫回来。
        # 只写 Qt.Window 的话按钮组取决于平台默认值，这里写全，行为才确定。
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(*self.DEFAULT_SIZE)

        self._headings: List[Heading] = []
        self._block_by_slug: Dict[str, QTextBlock] = {}
        self._item_by_slug: Dict[str, QTreeWidgetItem] = {}
        self._current_slug = ""
        self._loaded_lang = ""
        # 目录与正文互相驱动，必须有个闸：否则「选中目录 → 滚动正文 → 反向高亮目录」
        # 会绕成环。
        self._syncing = False

        # 搜索状态。命中列表存的是光标快照，换文档（切语言）后坐标就失效了，
        # 所以 reload 里会清空。
        self._search_query = ""
        self._search_matches: List[QTextCursor] = []
        self._search_index = -1

        self._build_ui()
        # 设置窗口里改语言时，已经打开的说明书跟着换（否则界面上是英文、说明书还是
        # 中文，得关掉重开才对）。show_help 里还有一层兜底比较：窗口关着的时候
        # 换语言，下次打开也会重读。
        self.i18n.language_changed.connect(self._on_language_changed)

    # ---- 对外 ----

    def show_help(self) -> bool:
        """按当前语言刷新内容并显示。文档缺失时返回 False，由调用方决定怎么提示。"""
        if self._loaded_lang != self.i18n.get_language() or not self._headings:
            if not self.reload():
                return False
        # 独立窗口可以被最小化：show() 对最小化窗口只是「保持最小化」，得先还原，
        # 否则点菜单会得到「什么都没发生」。
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        return True

    def reload(self) -> bool:
        """按当前语言重读说明书并重建目录。文件不存在返回 False。"""
        lang = self.i18n.get_language()
        path = guide_path(lang)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("无法读取帮助文档 %s: %s", path, exc)
            return False

        html, headings = build_html(text)

        self.setWindowTitle(self.i18n.tr("help.title"))
        self._headings = headings
        # baseUrl 指向说明书所在目录：文档里 `docs/xxx-design.md` 这类相对链接才能
        # 解析成真实文件路径（点开交给系统默认程序）。
        self.view.document().setBaseUrl(QUrl.fromLocalFile(str(path.parent) + "/"))
        self.view.setHtml(html)

        self._rebuild_tree(headings)
        self._index_blocks(headings)
        self._current_slug = ""
        self._reset_search()
        self.view.verticalScrollBar().setValue(0)
        if headings:
            self._select_slug(headings[0].slug)

        self._loaded_lang = lang
        return True

    def _on_language_changed(self, _lang: str) -> None:
        """界面语言切换：窗口开着就原地换一份说明书，关着就等下次打开再换。"""
        self.setWindowTitle(self.i18n.tr("help.title"))
        self.search_edit.setPlaceholderText(self.i18n.tr("help.search_placeholder"))
        self.search_edit.setToolTip(self.i18n.tr("help.search_tip"))
        self.search_prev.setToolTip(self.i18n.tr("help.search_prev"))
        self.search_next.setToolTip(self.i18n.tr("help.search_next"))
        if self.isVisible():
            self.reload()

    # ---- 界面 ----
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)

        # 左栏 = 搜索框 + 目录。宽度约束挂在容器上，不能只挂在树上，否则加了搜索框
        # 之后宽度就只约束住下半截。
        left = QWidget(self.splitter)
        left.setMinimumWidth(180)
        left.setMaximumWidth(380)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 6, 8)
        left_layout.setSpacing(6)

        search_row = QVBoxLayout()
        search_row.setSpacing(4)
        self.search_edit = QLineEdit(left)
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText(self.i18n.tr("help.search_placeholder"))
        self.search_edit.setToolTip(self.i18n.tr("help.search_tip"))
        search_row.addWidget(self.search_edit)
        left_layout.addLayout(search_row)

        # 计数和「上一处 / 下一处」单独一行。塞进搜索框旁边的话，222px 的可用宽度里
        # 输入框只剩 120px 左右，英文占位符直接被截断——宁可多占 26px 高度。
        nav_row = QHBoxLayout()
        nav_row.setSpacing(2)
        # 命中计数（「3/12」）。固定一点宽度，数字变化时按钮不会跟着抖。
        self.search_count = QLabel("", left)
        self.search_count.setMinimumWidth(38)
        self.search_count.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.search_count.setStyleSheet("QLabel { color: #888888; }")
        nav_row.addWidget(self.search_count)
        nav_row.addStretch(1)

        self.search_prev = QToolButton(left)
        self.search_prev.setText("↑")
        self.search_prev.setToolTip(self.i18n.tr("help.search_prev"))
        self.search_next = QToolButton(left)
        self.search_next.setText("↓")
        self.search_next.setToolTip(self.i18n.tr("help.search_next"))
        for button in (self.search_prev, self.search_next):
            # 无边框小按钮：不搜的时候它们只是两个安静的小箭头，不抢目录的注意力。
            button.setAutoRaise(True)
            button.setFixedSize(26, 22)
            button.setEnabled(False)   # 输入框为空时没什么可翻的
            nav_row.addWidget(button)
        left_layout.addLayout(nav_row)

        self.tree = QTreeWidget(left)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setTextElideMode(Qt.ElideRight)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        left_layout.addWidget(self.tree, 1)

        self.view = QTextBrowser(self.splitter)
        # 链接全部自己处理（见 _on_anchor_clicked）。开着默认导航的话，点正文里的
        # `docs/xxx.md` 会把说明书本身替换成别的内容——那是最难解释的一种 bug。
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setReadOnly(True)
        self.view.setStyleSheet("QTextBrowser { background: #ffffff; }")
        # 正文是拿来读的，不是拿来贴边的：留 22px 白边，行长也更舒服。
        self.view.document().setDocumentMargin(22)

        self.splitter.addWidget(left)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([236, self.DEFAULT_SIZE[0] - 236])
        root.addWidget(self.splitter)

        self.tree.currentItemChanged.connect(self._on_tree_current_changed)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        self.search_edit.returnPressed.connect(self._on_search_next)
        # Shift+Enter 往回翻：QLineEdit 只发一个 returnPressed，分不出方向，
        # 所以自己拦按键。
        self.search_edit.installEventFilter(self)
        self.search_prev.clicked.connect(self._on_search_prev)
        self.search_next.clicked.connect(self._on_search_next)
        # Ctrl+F 是所有人的肌肉记忆，焦点直接落到搜索框上，顺手全选旧词。
        find_shortcut = QShortcut(QKeySequence.Find, self)
        find_shortcut.activated.connect(self._focus_search)

        self.view.verticalScrollBar().valueChanged.connect(self._on_view_scrolled)
        self.view.anchorClicked.connect(self._on_anchor_clicked)

    def _rebuild_tree(self, headings: List[Heading]) -> None:
        """按标题级别搭目录树。层级跳变（h1 直接到 h3）挂到最近的祖先上。"""
        self._syncing = True
        self.tree.clear()
        self._item_by_slug.clear()

        stack: List[Tuple[int, QTreeWidgetItem]] = []
        for heading in headings:
            if heading.level > TOC_MAX_LEVEL:
                continue
            item = QTreeWidgetItem([heading.text])
            item.setData(0, Qt.UserRole, heading.slug)
            item.setToolTip(0, heading.text)
            while stack and stack[-1][0] >= heading.level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append((heading.level, item))
            self._item_by_slug[heading.slug] = item

        self.tree.expandAll()
        self._syncing = False

    def _index_blocks(self, headings: List[Heading]) -> None:
        """把锚点映射到正文里的标题块，用于按像素位置滚动。

        不走 ``scrollToAnchor``：它依赖 Qt 内部的锚点表，中文锚点上的行为不保证；
        而标题块和标题清单出自同一份文档、顺序一致，按序号配对最稳。数量对不上
        （理论上只会在 Qt 改写文档时发生）就退回按文本 slug 匹配。
        """
        blocks = [
            block
            for block in self._iter_blocks()
            if block.blockFormat().headingLevel()
        ]

        if len(blocks) == len(headings):
            self._block_by_slug = {h.slug: b for h, b in zip(headings, blocks)}
            return

        logger.warning(
            "标题数量不一致（渲染 %d / 文档 %d），退回按文本匹配锚点",
            len(headings), len(blocks),
        )
        seen: Dict[str, int] = {}
        self._block_by_slug = {}
        for block in blocks:
            slug = _unique_slug(block.text(), seen)
            self._block_by_slug.setdefault(slug, block)

    def _iter_blocks(self):
        block = self.view.document().begin()
        while block.isValid():
            yield block
            block = block.next()

    # ---- 滚动与跳转 ----

    def _scroll_to(self, slug: str, *, sync_tree: bool = True) -> None:
        block = self._block_by_slug.get(slug)
        if block is None:
            return

        bar = self.view.verticalScrollBar()
        top = self.view.document().documentLayout().blockBoundingRect(block).top()
        # 留一点余量，让标题离视口上沿有一行呼吸空间，而不是贴边顶着
        bar.setValue(max(0, int(top) - 8))

        # 上面那句依赖「布局已经是当前这一帧的」。刚 setHtml、刚拖过窗口大小的时候，
        # 文档布局还停在上一帧，算出来的位置是旧的，滚动就会落空。这里用光标定位兜底：
        # Qt 自己会在定位前把布局做到最新，行为一定正确（只是标题会贴边，不如上面好看）。
        if not (bar.value() <= top <= bar.value() + self.view.viewport().height()):
            self.view.setTextCursor(QTextCursor(block))
            self.view.ensureCursorVisible()

        if sync_tree:
            self._select_slug(slug)

    def _select_slug(self, slug: str) -> None:
        item = self._item_by_slug.get(slug)
        if item is None or item is self.tree.currentItem():
            return
        # 目录正在过滤时，当前章节可能被滤掉了。硬选一个隐藏项，Qt 会把它算进
        # scrollToItem 的坐标里，树就莫名其妙地跳一下——不选比乱跳好。
        if item.isHidden():
            return
        self._syncing = True
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self._syncing = False
        self._current_slug = slug

    def _on_tree_current_changed(self, current, previous) -> None:
        if self._syncing or current is None:
            return
        slug = current.data(0, Qt.UserRole)
        if not slug:
            return
        self._current_slug = slug
        self._scroll_to(slug, sync_tree=False)

    def _on_view_scrolled(self, value: int) -> None:
        """正文滚动时反向高亮目录：取最后一个「顶边已经越过视口上沿」的标题。"""
        if self._syncing or not self._block_by_slug:
            return

        bar = self.view.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 2:
            # 滚到底时最后一屏可能整屏都在末尾几节里：这时「越过上沿的最后一个标题」
            # 会停在倒数第几节上，而用户看到的明明是最后一节。
            current = next(reversed(self._item_by_slug), "")
        else:
            layout = self.view.document().documentLayout()
            current = ""
            for slug, block in self._block_by_slug.items():
                if layout.blockBoundingRect(block).top() - 40 > value:
                    break
                # h4 不在目录里（见 TOC_MAX_LEVEL），滚到那一带时保持父级高亮，
                # 否则目录会「什么都没选中」。
                if slug in self._item_by_slug:
                    current = slug
            # 停在文首时可能没有任何标题越过上沿（正文上边距比阈值大），
            # 这时高亮第一个标题，而不是留着上一次的选中项不刷新。
            if not current and self._item_by_slug:
                current = next(iter(self._item_by_slug))

        if current and current != self._current_slug:
            self._current_slug = current
            self._select_slug(current)

    # ---- 搜索 ----
    #
    # 一个输入框干两件事：输入时就地过滤目录（找章节），回车在正文里找词（找内容）。
    # 之所以不做「边输入边全文搜索」：中文常用字一打就几百个命中，正文跳来跳去比不搜
    # 还乱；回车才跳，用户对「我现在在找第几处」有完全的掌控。

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _on_search_text_changed(self, text: str) -> None:
        self._filter_outline(text)
        # 词变了，上一批命中（以及它们的光标坐标）都作废。
        self._reset_matches()
        self.search_count.clear()
        # 空词没什么可翻的；只要有字就亮起来，等用户点箭头或按回车。
        has_text = bool(text.strip())
        self.search_prev.setEnabled(has_text)
        self.search_next.setEnabled(has_text)
        if not has_text:
            # 目录重新长回来时，把「当前章节」的高亮补一次：过滤期间藏着的那一项，
            # 现在又该亮起来了。
            self._on_view_scrolled(self.view.verticalScrollBar().value())

    def _filter_outline(self, text: str) -> None:
        """目录按标题文本过滤，命中项的祖先一律保留。

        标题一个都没命中时**不清空目录**：用户要找的很可能是正文里的词，这时清空
        目录等于告诉他「这文档没这词」，还顺手把继续浏览的入口收走了。保持原样，
        回车去正文里找。
        """
        query = text.strip().lower()
        items = list(self._item_by_slug.values())
        if not query:
            for item in items:
                item.setHidden(False)
            return

        hits = [item for item in items if query in item.text(0).lower()]
        if not hits:
            for item in items:
                item.setHidden(False)
            return

        for item in items:
            item.setHidden(True)
        for item in hits:
            node = item
            while node is not None:
                node.setHidden(False)
                node = node.parent()

    def _reset_matches(self) -> None:
        self._search_query = ""
        self._search_matches = []
        self._search_index = -1
        self.view.setExtraSelections([])

    def _reset_search(self) -> None:
        """换文档（切语言）时调用：旧命中的光标坐标在新文档里毫无意义。"""
        self.search_edit.clear()   # 顺带触发布局过滤复位
        self.search_count.clear()
        self._reset_matches()

    def _collect_matches(self, query: str) -> List[QTextCursor]:
        """把全文命中一次性收集出来，供「上一处 / 下一处」轮流跳。

        上限 500 处：这份文档也就几千字，正常词不会到这个量级；真遇到「一」这种
        字，高亮几百个矩形只会拖慢滚动，而用户根本不会去数第 483 处。
        """
        doc = self.view.document()
        matches: List[QTextCursor] = []
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.Start)
        while len(matches) < 500:
            found = doc.find(query, cursor)
            if found.isNull():
                break
            matches.append(found)
            cursor = found
        return matches

    def _on_search_next(self) -> None:
        """往下找：回车、↓ 按钮、点计数之后的第一处。到尾部绕回开头。"""
        self._walk_matches(1)

    def _on_search_prev(self) -> None:
        """往上找：Shift+Enter 或 ↑ 按钮。到开头绕回结尾。"""
        self._walk_matches(-1)

    def _walk_matches(self, step: int) -> None:
        query = self.search_edit.text().strip()
        if not query:
            return
        if query != self._search_query:
            self._search_query = query
            self._search_matches = self._collect_matches(query)
            # 首次查找：往下走落到第一处，往上走落到最后一处——和浏览器里的直觉一致，
            # 也免得「往上找」第一下却是跳到开头。
            self._search_index = 0 if step < 0 else -1

        if not self._search_matches:
            self.search_count.setText("0/0")
            return

        self._search_index = (self._search_index + step) % len(self._search_matches)
        self.search_count.setText(
            f"{self._search_index + 1}/{len(self._search_matches)}"
        )
        self._highlight_matches()
        self._jump_to_match(self._search_matches[self._search_index])

    def eventFilter(self, obj, event):
        """Shift+Enter 往上找。QLineEdit 的 returnPressed 不区分 Shift，只能自己拦。"""
        if obj is self.search_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (
                event.modifiers() & Qt.ShiftModifier
            ):
                self._on_search_prev()
                return True
        return super().eventFilter(obj, event)

    def _highlight_matches(self) -> None:
        """所有命中铺一层浅黄底，当前那处由选区盖在上面——不用维护两套颜色。"""
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ffeaa7"))
        selections = []
        for match in self._search_matches:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = match
            selection.format = fmt
            selections.append(selection)
        self.view.setExtraSelections(selections)

    def _jump_to_match(self, match: QTextCursor) -> None:
        """把命中处挪到视口上方三分之一处。

        不用「先 ensureCursorVisible 再往回减」的写法：命中在视口上方时（绕回第一处），
        ensureCursorVisible 会把它贴到上沿，再往回减就直接减出屏幕了。按光标矩形算
        位移则不分方向，落在哪都摆到同一个高度。
        """
        self.view.setTextCursor(match)
        bar = self.view.verticalScrollBar()
        offset = self.view.cursorRect().top() - self.view.viewport().height() // 3
        bar.setValue(bar.value() + offset)

    def _on_anchor_clicked(self, url: QUrl) -> None:
        """正文链接：文档内锚点自己滚，其余交给系统默认程序打开。"""
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return

        fragment = url.fragment()
        if fragment:
            slug = fragment if fragment in self._block_by_slug else unquote(fragment, encoding="utf-8")
            if slug in self._block_by_slug:
                self._scroll_to(slug)
                return

        if url.isLocalFile():
            # 相对链接（如 docs/text-dictionary-design.md）已被 baseUrl 解析成绝对路径
            QDesktopServices.openUrl(url)
            return

        logger.debug("忽略无法处理的帮助链接: %s", url.toString())
