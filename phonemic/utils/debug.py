"""调试模式开关：显式开启，默认关闭。

历史包袱：过去拿「是不是从源码运行」当「要不要调试」的替身（各调试入口写的是
``not is_frozen()``）。这两件事本来无关：

- 从源码直接运行，多数时候只是想用它，不该凭空多出日志浮层、/test 页面和
  鼠标调试窗口；
- 反过来，打包版真出问题时恰恰更需要这些诊断入口。

于是收敛成一个显式开关，两种打开方式（等价）：

    PHONEMIC_DEBUG=1 python -m phonemic.PhoneMic     # 环境变量
    python -m phonemic.PhoneMic --debug              # 命令行

默认（不设环境变量、不带参数）一律**不是**调试模式。打包版同理——只看开关，
不再由 is_frozen() 代劳。

环境变量每次调用时现读，改完重启进程即生效；命令行通过 set_debug_mode() 落一个
覆盖值，优先级高于环境变量（用户显式敲的参数不该被环境里遗留的变量顶掉）。
"""

import os

# 环境变量名。与 PHONEMIC_LOG / PHONEMIC_WS_DEBUG 同一命名习惯。
DEBUG_ENV = "PHONEMIC_DEBUG"

# 只认「明确开启」的取值；其余（空串、"0"、"false"、拼错的值）一律按关闭处理。
# 调试入口的默认一侧必须是关闭：写错了就不多开东西，而不是悄悄多开。
_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}

# 命令行显式表态的结果。三态是必要的：如果默认就存 False，会盖掉环境变量。
_override: bool | None = None


def set_debug_mode(enabled: bool = True) -> None:
    """设置进程级调试开关（命令行 --debug 走的这条路）。"""
    global _override
    _override = bool(enabled)


def reset_debug_mode() -> None:
    """清掉命令行覆盖，回到「只看环境变量」。主要给测试用。"""
    global _override
    _override = None


def is_debug_mode() -> bool:
    """当前是否处于调试模式，默认 False。"""
    if _override is not None:
        return _override
    return os.environ.get(DEBUG_ENV, "").strip().lower() in _TRUE_VALUES
