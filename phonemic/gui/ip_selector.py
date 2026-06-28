from typing import List
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QDialogButtonBox, QWidget, QMessageBox
)
from phonemic.utils.network import get_all_lan_ips
from phonemic.utils.i18n import I18n

# IpCandidate 类型定义（实际应来自 utils.network，此处保留原导入逻辑）
try:
    from phonemic.utils.network import IpCandidate
except ImportError:
    from typing import NamedTuple
    class IpCandidate(NamedTuple):
        ip: str
        description: str
        priority: int


class IpSelector(QDialog):
    """IP 选择对话框，用于多 IP 场景下让用户手动选择绑定地址"""

    def __init__(self, candidates: List[IpCandidate], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.i18n = I18n.instance()

        self.setWindowTitle(self.i18n.tr("ip_selector.title"))
        self.setModal(True)
        self.resize(450, 350)

        # 按 priority 升序排序
        sorted_candidates = sorted(candidates, key=lambda c: c.priority)

        # 保存排序后的列表（用于 get_selected_ip）
        self._candidates = sorted_candidates

        # 布局
        layout = QVBoxLayout(self)

        # 提示标签（国际化）
        info_label = QLabel(self.i18n.tr("ip_selector.info"))
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # 列表控件
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.list_widget)

        # 填充列表
        for cand in sorted_candidates:
            # 显示文本：描述 - IP，也可以完全国际化，但 IP 和描述本身无需翻译
            item = QListWidgetItem(f"{cand.description} - {cand.ip}")
            item.setData(Qt.UserRole, cand.ip)
            self.list_widget.addItem(item)

        # 默认选中第一项
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        # 按钮（Ok/Cancel 使用标准按钮，Qt 会自动处理翻译，无需手动 tr）
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # 双击列表项等同于确定
        self.list_widget.itemDoubleClicked.connect(self.accept)

    def get_selected_ip(self) -> str | None:
        """获取用户选中的 IP 地址，仅在 exec() 返回后调用有效"""
        if self.result() != QDialog.Accepted:
            return None

        current_item = self.list_widget.currentItem()
        if current_item is None:
            return None

        ip = current_item.data(Qt.UserRole)
        return ip if isinstance(ip, str) else None

def select_lan_ip(parent: QWidget | None = None) -> str | None:
    """
    弹出 IP 选择对话框，返回用户选择的 IP 地址。
    若取消或没有可用 IP，返回 None。
    """
    candidates = get_all_lan_ips()
    if not candidates:
        QMessageBox.critical(parent, "错误", "未检测到可用局域网IP，请检查网络连接。")
        return None
    if len(candidates) == 1:
        return candidates[0].ip
    selector = IpSelector(candidates, parent)
    if selector.exec() != QDialog.Accepted:
        return None
    return selector.get_selected_ip()
