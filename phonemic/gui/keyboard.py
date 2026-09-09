"""
上屏逻辑：把文本送到当前光标位置，两种方式可选——
- 剪贴板粘贴：写剪贴板 + 模拟 Ctrl+V，随后恢复原剪贴板内容；
- 模拟键盘输入：Win32 SendInput 逐字符注入，不碰剪贴板，
  适用于终端 / SSH 客户端 / vim 等 Ctrl+V 不生效的场景。

同时提供按键序列执行功能（支持逗号分隔多个组合）。
"""
import logging
import time
from typing import Optional, Tuple, List

import pyautogui
import pyperclip

from phonemic.gui import text_input

logger = logging.getLogger(__name__)

# 上屏方式取值
INPUT_MODE_PASTE = "paste"   # 剪贴板 + Ctrl+V
INPUT_MODE_TYPE = "type"     # 模拟键盘逐字符输入
VALID_INPUT_MODES = (INPUT_MODE_PASTE, INPUT_MODE_TYPE)
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
    将文本送到当前光标位置，具体方式由配置的上屏方式决定。

    模拟输入若一个字符都没能注入（例如目标窗口以管理员权限运行，SendInput 被
    UIPI 拦截），回退到剪贴板粘贴；若已注入了一部分则不回退，否则会重复上屏。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if text == "":
        logger.warning("flash_insert called with empty text, doing nothing")
        return

    if get_input_mode() == INPUT_MODE_TYPE:
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
    组合之间会插入 0.05 秒的短暂延迟。
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
    for combo in combos:
        parts = combo.lower().split('+')
        try:
            pyautogui.hotkey(*parts)
            logger.debug(f"执行组合: {combo}")
            time.sleep(0.05)  # 组合之间的短暂延迟
        except Exception as e:
            logger.exception(f"模拟按键失败，组合: {combo} - {e}")
            # 发生错误时停止后续执行，避免状态混乱
            break

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