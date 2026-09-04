"""
直接模拟键盘输入文本（Win32 SendInput + KEYEVENTF_UNICODE）。

为什么需要它：
剪贴板 + Ctrl+V 的上屏方式在不少场景下不可用——终端与 SSH 客户端
（cmd、PowerShell、Windows Terminal、PuTTY、mintty、ConEmu 等）的粘贴快捷键
往往不是 Ctrl+V，Ctrl+V 会被当作 ^V 控制字符；vim 等模式编辑器、部分
安全输入框也会屏蔽粘贴。

原理：
KEYEVENTF_UNICODE 让 SendInput 把 wScan 中的 UTF-16 码元以 VK_PACKET 的形式
注入键盘输入流，TranslateMessage 随后生成对应的 WM_CHAR。对目标程序而言
与真人逐字敲入等价，既不依赖剪贴板，也不污染剪贴板。

已知边界：
- BMP 之外的字符（emoji）必须按 UTF-16 代理项对拆成两个码元连续发送。
- SendInput 受 UIPI 限制：目标窗口以更高完整性级别（管理员）运行时注入会失败，
  此时 send_text 抛 SendTextError，由调用方决定是否回退到剪贴板方案。
"""
import ctypes
import logging
import sys
import time
from typing import List

logger = logging.getLogger(__name__)


class SendTextError(RuntimeError):
    """
    模拟输入失败。

    partial=True 表示已有部分字符注入到目标窗口，此时若再回退到剪贴板粘贴，
    这部分文字会重复上屏，因此调用方必须放弃回退。
    """

    def __init__(self, message: str, partial: bool = False):
        super().__init__(message)
        self.partial = partial

IS_WINDOWS = sys.platform == "win32"

# ---------- Win32 结构体定义 ----------
if IS_WINDOWS:
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
else:  # pragma: no cover - 仅为让模块在非 Windows 平台可导入（测试/静态检查）
    INPUT = None
    _user32 = None

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_TAB = 0x09
VK_RETURN = 0x0D

# 单次 SendInput 调用注入的最大事件数。SendInput 内部是原子的，一次调用越多
# 越能保证顺序；但整段文本可能很长，分批可避免一次分配过大的数组。
MAX_INPUTS_PER_CALL = 400
# 批次之间的间隔。ConPTY / 部分终端在极高速注入下会丢字符，留一个极小的间隙。
BATCH_INTERVAL_SEC = 0.002


def _make_input(w_vk: int, w_scan: int, flags: int) -> "INPUT":
    """构造一个键盘 INPUT 事件"""
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.union.ki.wVk = w_vk
    item.union.ki.wScan = w_scan
    item.union.ki.dwFlags = flags
    item.union.ki.time = 0
    item.union.ki.dwExtraInfo = 0
    return item


def _unicode_pair(code_unit: int) -> List["INPUT"]:
    """一个 UTF-16 码元的按下 + 抬起事件"""
    return [
        _make_input(0, code_unit, KEYEVENTF_UNICODE),
        _make_input(0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def _vk_pair(vk: int) -> List["INPUT"]:
    """一个虚拟键的按下 + 抬起事件"""
    return [
        _make_input(vk, 0, 0),
        _make_input(vk, 0, KEYEVENTF_KEYUP),
    ]


def _char_inputs(ch: str) -> List["INPUT"]:
    """
    单个字符对应的事件序列。

    - 换行走 VK_RETURN（Unicode 注入 \\n 在多数程序里不会产生回车动作）
    - 制表符走 VK_TAB（终端里的补全、编辑器里的缩进才符合"真人敲键"预期）
    - 其余控制字符丢弃
    - BMP 外字符按 UTF-16 代理项对拆成两个码元
    """
    if ch == "\n":
        return _vk_pair(VK_RETURN)
    if ch == "\t":
        return _vk_pair(VK_TAB)
    if ord(ch) < 0x20 or ord(ch) == 0x7F:
        return []

    inputs: List["INPUT"] = []
    raw = ch.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        inputs.extend(_unicode_pair(raw[i] | (raw[i + 1] << 8)))
    return inputs


def _batch(groups: List[List["INPUT"]]) -> List[List["INPUT"]]:
    """把逐字符的事件分组打包成批次，保证同一个字符（含代理项对）不被拆开"""
    batches: List[List["INPUT"]] = []
    current: List["INPUT"] = []
    for group in groups:
        if current and len(current) + len(group) > MAX_INPUTS_PER_CALL:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches


def _send(events: List["INPUT"]) -> None:
    """调用 SendInput 注入一批事件，未全部注入则抛 SendTextError"""
    count = len(events)
    array = (INPUT * count)(*events)
    sent = _user32.SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        err = ctypes.get_last_error()
        raise SendTextError(
            f"SendInput 注入失败: 期望 {count} 个事件，实际 {sent} 个 (WinError {err})。"
            "目标窗口以管理员权限运行时需要 PhoneMic 同样以管理员权限运行。",
            partial=sent > 0,
        )


def send_text(text: str) -> None:
    """
    以模拟键盘的方式逐字符输入文本，不使用剪贴板。

    失败时抛 SendTextError；其 partial 属性为 False 时表示一个字符都没进去，
    调用方可以安全地回退到剪贴板粘贴。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text:
        logger.warning("send_text called with empty text, doing nothing")
        return
    if not IS_WINDOWS:
        raise SendTextError("模拟键盘输入仅在 Windows 上可用")

    # 统一换行，避免 \r\n 触发两次回车
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    groups = [g for g in (_char_inputs(ch) for ch in normalized) if g]
    if not groups:
        logger.warning("send_text: 文本不含可输入字符，跳过")
        return

    batches = _batch(groups)
    for index, batch in enumerate(batches):
        if index:
            time.sleep(BATCH_INTERVAL_SEC)
        try:
            _send(batch)
        except SendTextError as e:
            # 第一批之后再失败，前面的字符已经上屏，回退粘贴会造成重复
            if index and not e.partial:
                raise SendTextError(str(e), partial=True) from e
            raise
    logger.debug(f"模拟输入完成: {len(normalized)} 字符 / {len(batches)} 批")
