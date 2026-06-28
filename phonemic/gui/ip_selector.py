from typing import List, Optional, Tuple
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QDialogButtonBox, QWidget, QMessageBox
)
from phonemic.utils.network import get_all_lan_ips, IpCandidate
from phonemic.utils.i18n import I18n

# IpCandidate 已在 network 中定义，无需再定义


class IpSelector(QDialog):
    """IP 选择对话框，用于多 IP 场景下让用户手动选择绑定地址"""

    def __init__(self, candidates: List[IpCandidate], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.i18n = I18n.instance()
        self._candidates = candidates  # 保存原始候选列表
        self._selected_index = -1      # 默认为未选择

        self.setWindowTitle(self.i18n.tr("ip_selector.title"))
        self.setModal(True)
        self.resize(450, 350)

        # 按 priority 升序排序
        sorted_candidates = sorted(candidates, key=lambda c: c.priority)
        self._candidates = sorted_candidates

        layout = QVBoxLayout(self)

        info_label = QLabel(self.i18n.tr("ip_selector.info"))
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.list_widget)

        # 填充列表：显示描述 - IP，存储索引
        for idx, cand in enumerate(sorted_candidates):
            item = QListWidgetItem(f"{cand.description} - {cand.ip}")
            item.setData(Qt.UserRole, idx)   # 存储索引
            self.list_widget.addItem(item)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.list_widget.itemDoubleClicked.connect(self.accept)

    def get_selected_candidate(self) -> Optional[IpCandidate]:
        """获取用户选中的候选对象，若未选中或取消则返回 None"""
        if self.result() != QDialog.Accepted:
            return None
        current_row = self.list_widget.currentRow()
        if current_row < 0 or current_row >= len(self._candidates):
            return None
        return self._candidates[current_row]

    # 为了兼容性，保留旧方法（但会废弃）
    def get_selected_ip(self) -> Optional[str]:
        cand = self.get_selected_candidate()
        return cand.ip if cand else None


def select_lan_ip(parent: QWidget | None = None) -> Tuple[Optional[str], Optional[str]]:
    """
    弹出 IP 选择对话框，返回用户选择的 (ip, mac)。
    若取消或没有可用 IP，返回 (None, None)。
    """
    candidates = get_all_lan_ips()
    if not candidates:
        QMessageBox.critical(parent, "错误", "未检测到可用局域网IP，请检查网络连接。")
        return None, None
    if len(candidates) == 1:
        return candidates[0].ip, candidates[0].mac
    selector = IpSelector(candidates, parent)
    if selector.exec() != QDialog.Accepted:
        return None, None
    selected = selector.get_selected_candidate()
    if selected is None:
        return None, None
    return selected.ip, selected.mac