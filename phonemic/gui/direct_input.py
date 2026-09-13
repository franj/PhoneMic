"""
直接输入模式：识别过程中的文字即时打进目标输入框。

与「模拟键盘」模式的区别只在**时机**，注入手段都是 Win32 SendInput 逐字符，都不碰剪贴板：

- 模拟键盘（type）：只在识别结束（send）时一次性把整段文字敲进去，
  识别过程中文字只出现在悬浮窗；
- 直接输入（direct）：每来一次 preview 就增量更新目标输入框，文字像输入法
  一样边说边出现在光标处，send 时只做最后修正并落定，全程不显示悬浮窗。

增量更新用「公共前缀 diff」：语音识别的结果通常是往后追加或末尾修正，
只需退格删掉差异部分、再敲新增部分。实测一句话 10~50 字，几十次按键在
毫秒级完成，低于人眼 100ms 的感知门槛。

焦点保护：
会话开始时记下前台窗口 + 焦点控件的句柄（指纹），每次更新前比对。指纹变了
说明用户切了窗口或点了别的输入框——继续打字会把文字送到错误的位置，因此
立即放弃本次会话，由调用方退回悬浮窗显示。
"""
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Callable, Optional, Tuple

from phonemic.gui import text_input

logger = logging.getLogger(__name__)

# 会话状态
IDLE = "idle"              # 没有会话
TYPING = "typing"          # 会话进行中，已往目标输入框打过字
ABANDONED = "abandoned"    # 焦点丢失 / 注入失败，本次会话已放弃

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover - 非 Windows 仅为可导入

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    # pywin32 没有封装 GetGUIThreadInfo，这里和 text_input 一样走 ctypes 直接调 User32
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO))
    _user32.GetGUIThreadInfo.restype = wintypes.BOOL
else:  # pragma: no cover
    _GUITHREADINFO = None
    _user32 = None


def _focus_signature() -> Tuple[int, int]:
    """当前焦点指纹：(前台窗口句柄, 焦点控件句柄)。

    GetGUIThreadInfo(0) 取的是前台线程的信息。拿不到焦点控件时退化为只按
    前台窗口判定（多数场景够用）——hwndFocus 为 0 时按 0 参与比对即可。
    """
    if not IS_WINDOWS:  # pragma: no cover
        return 0, 0

    hwnd = _user32.GetForegroundWindow() or 0
    focus = 0
    try:
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if _user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            focus = info.hwndFocus or 0
    except Exception as e:
        logger.debug(f"读取焦点控件失败，只按前台窗口判定: {e}")
    return hwnd, focus


def _common_prefix_len(a: str, b: str) -> int:
    """公共前缀长度（按 Unicode 码点计，emoji 等占一个码点）。"""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


class DirectInputController:
    """
    一次「说话」的直接输入会话。

    生命周期：第一次非空 preview 开启 → 若干次增量更新 → send 提交结束；
    中途焦点丢失或注入失败则放弃（已上屏的文字留在原地，不做危险删除）。
    """

    def __init__(self, focus_probe: Callable[[], Tuple[int, int]] = _focus_signature):
        self._focus_probe = focus_probe
        self._state = IDLE
        self._typed = ""                      # 已打进目标输入框的文字
        self._dirty = False                   # 本次会话是否真的注入过文字
        self._anchor: Optional[Tuple[int, int]] = None

    # ---------- 只读状态（主要给测试用） ----------
    @property
    def state(self) -> str:
        return self._state

    @property
    def typed(self) -> str:
        return self._typed

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---------- 会话流程 ----------
    def update(self, text: str) -> bool:
        """
        识别过程中的增量更新。

        返回 True 表示文字已打进目标输入框，调用方不必再显示悬浮窗；
        返回 False 表示交给调用方按普通方式处理（还没开始会话 / 会话已放弃）。
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        if self._state == ABANDONED:
            return False

        if self._state == IDLE:
            if text == "":
                # 还没有任何东西要打，没必要为此开启会话
                return False
            self._begin()

        if not self._check_focus():
            return False

        try:
            self._apply(text)
        except text_input.SendTextError as e:
            self._abandon(f"注入失败: {e}")
            # 一个字符都没进去时调用方可以安全回退；否则回退会造成重复上屏
            if not e.partial and not self._dirty:
                return False
            raise
        return True

    def commit(self, text: str) -> bool:
        """
        识别结束：把已打的文字修正成最终文本，并结束会话。

        返回 True 表示文字已经（至少部分）在目标输入框里，调用方**不要再做任何
        上屏动作**；返回 False 表示本次压根没有会话（例如客户端只发了 send、
        或会话刚开启就丢了焦点），调用方按普通方式上屏即可。
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        if self._state == IDLE:
            return False

        if self._state == ABANDONED:
            return self._commit_abandoned()

        if not self._check_focus():
            return self._commit_abandoned()

        try:
            self._apply(text)
        except text_input.SendTextError as e:
            self._abandon(f"提交失败: {e}")
            if not e.partial and not self._dirty:
                return False
            raise
        logger.debug(f"直接输入提交完成: {len(text)} 字符")
        self.reset()
        return True

    def discard(self) -> bool:
        """
        撤销本次会话已经打进输入框的文字（命中语音命令时用）。

        命令执行的往往只是一个回车或一组快捷键，preview 阶段打出的「回车」
        这类字面文字必须先删干净，否则会和命令的效果一起留在目标程序里。

        返回 True 表示已清理（或压根没打过字）；返回 False 表示没法安全删除——
        **焦点已经不在锚点上了，此时绝不能发退格**，那会删掉新窗口里用户自己的内容。
        """
        if self._state == IDLE:
            return True
        if not self._dirty:
            self.reset()
            return True
        if self._state == ABANDONED or not self._check_focus():
            logger.warning("焦点已不在会话锚点，跳过撤销：已上屏的文字留在原处")
            self.reset()
            return False

        count = len(self._typed)
        try:
            text_input.send_backspace(count)
        except text_input.SendTextError as e:
            self._abandon(f"撤销失败: {e}")
            self.reset()
            logger.error(f"撤销直接输入失败，剩余文字留在目标输入框: {e}")
            return False
        logger.debug(f"已撤销直接输入: {count} 字符")
        self.reset()
        return True

    def reset(self) -> None:
        """结束并清空会话状态（不会动已经上屏的文字）。"""
        self._state = IDLE
        self._typed = ""
        self._dirty = False
        self._anchor = None

    # ---------- 内部 ----------
    def _begin(self) -> None:
        """开启会话，锚定当前焦点。"""
        self._anchor = self._focus_probe()
        self._state = TYPING
        self._typed = ""
        self._dirty = False
        logger.debug(f"直接输入会话开始，焦点指纹={self._anchor}")

    def _check_focus(self) -> bool:
        """焦点是否还在会话开始时锚定的位置，不在则放弃会话。"""
        current = self._focus_probe()
        if self._anchor is not None and current != self._anchor:
            self._abandon(f"焦点已从 {self._anchor} 变为 {current}")
            return False
        return True

    def _commit_abandoned(self) -> bool:
        had_text = self._dirty
        self.reset()
        if had_text:
            logger.warning("直接输入会话已放弃但文字曾部分上屏，本次不再补发，避免重复")
        return had_text

    def _abandon(self, reason: str) -> None:
        logger.warning(f"放弃直接输入会话：{reason}，剩余文字转由悬浮窗显示")
        self._state = ABANDONED

    def _apply(self, text: str) -> None:
        """公共前缀 diff：退格删掉差异部分，再敲新增部分。"""
        keep = _common_prefix_len(self._typed, text)
        remove = len(self._typed) - keep
        if remove:
            text_input.send_backspace(remove)
        add = text[keep:]
        if add:
            text_input.send_text(add)
        if remove or add:
            self._dirty = True
        self._typed = text


# ---------- 模块级单例 ----------
# 输入状态天然是全局的（同一时刻只有一处焦点），按项目惯例惰性创建，
# 模块级只留一个引用 + getter。
_controller: Optional[DirectInputController] = None


def get_controller() -> DirectInputController:
    """获取当前（必要时创建）直接输入控制器。"""
    global _controller
    if _controller is None:
        _controller = DirectInputController()
    return _controller


def update(text: str) -> bool:
    """增量更新（preview 事件）。语义见 DirectInputController.update。"""
    return get_controller().update(text)


def commit(text: str) -> bool:
    """提交最终结果（send 事件）。语义见 DirectInputController.commit。"""
    return get_controller().commit(text)


def discard() -> bool:
    """撤销已上屏的 preview 文字（命令命中时）。语义见 DirectInputController.discard。"""
    return get_controller().discard()


def reset() -> None:
    """结束当前会话（供模式切换、测试隔离调用）。"""
    get_controller().reset()
