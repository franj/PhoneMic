"""
系统语言检测模块，用于首次运行时确定默认界面语言。
返回原始语言代码（如 'zh_CN', 'zh_TW', 'en_US'），不做语言变体归一化。

不用 locale.getdefaultlocale()，两个原因：
- 它把环境变量当答案。Windows 上 LANG/LC_ALL 常由 git-bash、WSL、IDE 顺手带上，
  与系统语言无关（实测本机是 LC_ALL=C.UTF-8 / LANG=en_US.UTF-8，系统却是中文），
  照它选会选错语言包。
- 它在 Python 3.13/3.14 上发 DeprecationWarning（3.15 虽已收回该弃用，但没必要等）。

改为按平台直接问操作系统：Windows 走 Win32 API，其余走 POSIX 语言环境变量。
"""

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# POSIX 语言环境变量，按优先级排列。POSIX 规定 LC_ALL 覆盖一切，所以只认
# 「第一个有值的」：LC_ALL=C 表示「不要本地化」，此时 LANG 不作数。
_POSIX_LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")

# 'C' / 'POSIX' 不是语言，只表示「没有语言偏好」，不能当成检测结果
_NOT_A_LANGUAGE = {"c", "posix"}


def _normalize_language_code(raw: Optional[str]) -> Optional[str]:
    """
    把平台给出的语言标识规整成「语言_地区」形式。

    只做格式规整，不做语言变体归一化：
    'zh-CN' -> 'zh_CN'；'zh_CN.UTF-8' -> 'zh_CN'；'sr_RS@latin' -> 'sr_RS'。
    无法当作语言识别的（None / 空串 / 'C' / 'C.UTF-8' / 'POSIX'）返回 None。
    """
    if not raw or not isinstance(raw, str):
        return None
    # 先剥编码（.UTF-8 / .936），再剥修饰符（@latin / @euro）
    code = raw.strip().split(".", 1)[0].split("@", 1)[0].strip()
    if not code or code.lower() in _NOT_A_LANGUAGE:
        return None
    return code.replace("-", "_")


def _detect_on_windows() -> Optional[str]:
    """
    Windows：取当前用户的 UI 语言（系统菜单/对话框所用语言）。

    用 UI 语言而不是用户区域：两者可以不同（英文系统 + 中国区域，或反过来），
    而这里要的是界面语言。返回的 LANGID 经 locale.windows_locale 翻成 POSIX
    形式（2052 -> 'zh_CN'），与语言包文件名一致；该表由 CPython 跟随 Windows
    LCID 规范更新（当前覆盖到协议修订 16.0 / 2024-04-23）。

    不读环境变量：Windows 上 LANG/LC_ALL 与系统语言无关（见模块 docstring）。
    """
    import ctypes
    import locale

    # 0x1000 = LOCALE_CUSTOM_UI_DEFAULT：UI 语言来自语言包且属补充区域时返回它，
    # 表里没有对应项，此时返回 None，由调用方回落 en_US。
    langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    code = locale.windows_locale.get(langid)
    if not code:
        logger.debug(f"unmapped Windows UI language id: 0x{langid:04x}")
    return code


def _detect_on_posix() -> Optional[str]:
    """
    POSIX（Linux/macOS）：按优先级取第一个有值的语言环境变量。

    不用 locale.getlocale() 代替：它返回的是「当前已 setlocale 的 LC_CTYPE」，
    反映的是编码而不是界面语言，且进程启动后可能被别的库改掉。
    """
    for var in _POSIX_LOCALE_VARS:
        raw = os.environ.get(var)
        if raw:
            code = _normalize_language_code(raw)
            logger.debug(f"detected via {var}={raw!r}: {code}")
            return code
    return None


def detect_system_language() -> str:
    """
    检测操作系统当前的语言设置，返回原始语言代码（如 'zh_CN', 'zh_TW', 'en_US'）。
    若无法检测，默认返回 'en_US'。

    返回值格式：语言_地区，下划线分隔，例如 'zh_CN', 'zh_TW', 'en_US'。
    """
    try:
        detector = _detect_on_windows if sys.platform == "win32" else _detect_on_posix
        lang_code = detector()
    except Exception as e:
        logger.warning(f"system language detection failed: {e}")
        lang_code = None

    # 最终回退
    if not lang_code:
        logger.warning("Unable to detect system language, falling back to en_US")
        return "en_US"

    # 不做归一化，直接返回原始代码（例如 zh_CN, zh_TW, en_US, de_DE 等）
    return lang_code
