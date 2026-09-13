"""
上屏逻辑：把文本送到当前光标位置，三种方式可选——
- 剪贴板粘贴：写剪贴板 + 模拟 Ctrl+V，随后恢复原剪贴板内容；
- 模拟键盘输入：Win32 SendInput 逐字符注入，不碰剪贴板，
  适用于终端 / SSH 客户端 / vim 等 Ctrl+V 不生效的场景；
- 模拟键盘（直接输入）：同样是 SendInput 逐字符注入，但识别过程中就把文字
  打进输入框（preview 事件增量更新），send 时只做最后修正，全程不显示悬浮窗。

同时提供按键序列执行功能（支持逗号分隔多个组合）。
"""
import logging
import time
from typing import Optional, Tuple, List

import pyautogui
import pyperclip

from phonemic.gui import direct_input, text_input

logger = logging.getLogger(__name__)

# 上屏方式取值
INPUT_MODE_PASTE = "paste"     # 剪贴板 + Ctrl+V
INPUT_MODE_TYPE = "type"       # 模拟键盘逐字符输入（只在识别结束时上屏）
INPUT_MODE_DIRECT = "direct"   # 模拟键盘 + 识别过程中直接输入（边说边出字）
VALID_INPUT_MODES = (INPUT_MODE_PASTE, INPUT_MODE_TYPE, INPUT_MODE_DIRECT)
DEFAULT_INPUT_MODE = INPUT_MODE_PASTE

# 显式指定时优先于配置文件，供命令行/测试覆盖
_input_mode_override: Optional[str] = None


def set_input_mode_override(mode: Optional[str]) -> None:
    """强制指定上屏方式；传 None 恢复为读取配置"""
    global _input_mode_override
    if mode is not None and mode not in VALID_INPUT_MODES:
        raise ValueError(f"未知上屏方式: {mode}")
    _input_mode_override = mode


def get_input_mode() -> str:
    """当前生效的上屏方式"""
    if _input_mode_override is not None:
        return _input_mode_override
    try:
        from phonemic.utils.settings_manager import SettingsManager

        mode = SettingsManager.instance().get("text_input_mode", DEFAULT_INPUT_MODE)
    except Exception as e:
        logger.debug(f"读取上屏方式配置失败，使用默认值: {e}")
        return DEFAULT_INPUT_MODE
    return mode if mode in VALID_INPUT_MODES else DEFAULT_INPUT_MODE


# ---------- 上屏入口 ----------
def flash_insert(text: str) -> None:
    """
    将文本送到当前光标位置（识别结束时的最终文本），具体方式由配置决定。

    模拟输入若一个字符都没能注入（例如目标窗口以管理员权限运行，SendInput 被
    UIPI 拦截），回退到剪贴板粘贴；若已注入了一部分则不回退，否则会重复上屏。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if text == "":
        logger.warning("flash_insert called with empty text, doing nothing")
        return

    mode = get_input_mode()
    if mode == INPUT_MODE_DIRECT:
        # 直接输入：识别过程中文字已经打进输入框，这里只把已打的内容修正成最终结果。
        # 返回 False 说明压根没有进行中的会话（客户端只发 send / 会话已放弃且
        # 一个字都没上屏），此时退化为普通的模拟键盘输入。
        if direct_input.commit(text):
            return

    if mode in (INPUT_MODE_TYPE, INPUT_MODE_DIRECT):
        _type_with_clipboard_fallback(text)
        return

    flash_insert_via_clipboard(text)


def preview_text(text: str) -> bool:
    """
    识别过程中的中间结果（preview 事件）。

    只有直接输入模式会真正打字。返回 True 表示文字已进目标输入框，调用方不必
    再显示悬浮窗；返回 False 表示维持原逻辑（悬浮窗预览）。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if get_input_mode() != INPUT_MODE_DIRECT:
        return False
    try:
        return direct_input.update(text)
    except text_input.SendTextError as e:
        # 已经打了一部分时不能让后续再补发，这里只退回悬浮窗显示本次结果
        logger.error(f"直接输入失败，本次退回悬浮窗显示: {e}")
        return False


def discard_preview() -> bool:
    """
    撤销直接输入模式在 preview 阶段已经打进输入框的文字。

    命中语音命令时调用：命令往往只是按个回车、发个快捷键，preview 阶段打出的
    「回车」这类字面文字必须先删掉，否则会和命令一起留在目标程序里。
    其它上屏方式没有 preview 文字，这里是安全的空操作。
    """
    return direct_input.discard()


def _type_with_clipboard_fallback(text: str) -> None:
    """模拟键盘逐字符输入，一个字符都没注入时回退到剪贴板粘贴。"""
    try:
        text_input.send_text(text)
        return
    except text_input.SendTextError as e:
        if e.partial:
            logger.error(f"模拟键盘输入中断，已有部分文字上屏，不回退以免重复: {e}")
            raise
        logger.warning(f"模拟键盘输入失败，回退到剪贴板粘贴: {e}")
    flash_insert_via_clipboard(text)


# ---------- 剪贴板粘贴 ----------
def flash_insert_via_clipboard(text: str) -> None:
    """
    将文本粘贴到当前光标位置，并恢复原剪贴板内容。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if text == "":
        logger.warning("flash_insert_via_clipboard called with empty text, doing nothing")
        return

    original_clipboard = None
    try:
        original_clipboard = pyperclip.paste()
        logger.debug("Original clipboard content saved")

        pyperclip.copy(text)
        logger.debug(f"Text copied to clipboard: {text[:50]}...")

        pyautogui.hotkey('ctrl', 'v')
        logger.debug("Ctrl+V simulated")

    except pyperclip.PyperclipException as e:
        raise RuntimeError(f"Clipboard operation failed: {e}") from e
    except pyautogui.FailSafeException as e:
        raise RuntimeError(f"PyAutoGUI failsafe triggered: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error during flash_insert_via_clipboard: {e}") from e
    finally:
        if original_clipboard is not None:
            try:
                pyperclip.copy(original_clipboard)
                logger.debug("Original clipboard restored")
            except pyperclip.PyperclipException as e:
                logger.critical(f"Failed to restore original clipboard: {e}")
        else:
            logger.warning("No original clipboard to restore")

# ---------- 按键序列执行（新增功能） ----------
# 可用键名集合（来自 pyautogui）
VALID_KEYS = set(pyautogui.KEYBOARD_KEYS)
MODIFIERS = {'ctrl', 'shift', 'alt', 'win'}

def _validate_single_combination(keys: str) -> Tuple[bool, str]:
    """校验单个按键组合（不含逗号），返回 (是否合法, 错误信息)"""
    if not keys or not keys.strip():
        return False, "按键字符串不能为空"
    keys_lower = keys.strip().lower()
    parts = keys_lower.split('+')
    for part in parts:
        if part not in VALID_KEYS:
            if part not in MODIFIERS or len(parts) == 1:
                return False, f"未知键名: '{part}'，请使用 pyautogui 支持的键名"
    return True, ""

def validate_key_sequence(keys_sequence: str) -> Tuple[bool, str]:
    """
    校验按键序列（支持逗号分隔多个组合）。
    返回 (是否合法, 错误信息)
    """
    if not keys_sequence or not keys_sequence.strip():
        return False, "按键序列不能为空"

    # 按逗号分割，并去除每个组合的前后空格
    combos = [c.strip() for c in keys_sequence.split(',')]
    if len(combos) > 10:
        return False, f"按键序列最多支持10个组合，当前有{len(combos)}个"

    for combo in combos:
        ok, err = _validate_single_combination(combo)
        if not ok:
            return False, f"组合 '{combo}' 无效: {err}"
    return True, ""

# 保留旧函数名作为兼容（但推荐使用 validate_key_sequence）
def validate_key_combination(keys: str) -> Tuple[bool, str]:
    """兼容旧接口：单组合校验"""
    return _validate_single_combination(keys)

def send_keys(keys_sequence: str) -> None:
    """
    模拟按键序列，支持逗号分隔多个组合。
    例如: "ctrl+a, delete" -> 先 Ctrl+A 全选，再 Delete 删除。
          "ctrl+c, enter" -> 复制后回车。
    每个组合内部使用 '+' 连接键名（如 "ctrl+shift+esc"）。
    组合之间会插入 0.05 秒的短暂延迟；最后一个组合后面不再停顿。
    """
    if not keys_sequence or not keys_sequence.strip():
        logger.error("按键序列为空，不做任何操作")
        return

    # 校验整个序列
    ok, err = validate_key_sequence(keys_sequence)
    if not ok:
        logger.error(f"按键序列非法: {keys_sequence} - {err}")
        return

    # 分割序列
    combos = [c.strip() for c in keys_sequence.split(',')]
    for i, combo in enumerate(combos):
        parts = combo.lower().split('+')
        try:
            # _pause=False：pyautogui 默认每次调用后 sleep(PAUSE=0.1s)。手机端「按住连发」
            # 是 50ms 一个按键帧，这 0.1s 会让 PC 端消费不过来、事件越积越多，表现成
            # 「按住删得慢、松手后还在删」。鼠标路径（gui/mouse.py）早就关掉了它。
            pyautogui.hotkey(*parts, _pause=False)
            logger.debug(f"执行组合: {combo}")
        except Exception as e:
            logger.exception(f"模拟按键失败，组合: {combo} - {e}")
            # 发生错误时停止后续执行，避免状态混乱
            break
        # 只在组合之间停顿，给上一个组合留点落地时间；单个组合（连发场景）不必等
        if i < len(combos) - 1:
            time.sleep(0.05)

# ---------- 备用粘贴方案（保留原样） ----------
import win32con
import win32gui

def flash_insert_via_paste_message(text: str):
    """使用 WM_PASTE 消息的备用粘贴方法"""
    original = pyperclip.paste()
    pyperclip.copy(text)
    try:
        hwnd = win32gui.GetForegroundWindow()
        win32gui.SendMessage(hwnd, win32con.WM_PASTE, 0, 0)
    finally:
        pyperclip.copy(original)