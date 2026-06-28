# -*- coding: utf-8 -*-
r"""
开机自启动管理模块（Windows 注册表）
使用 Windows 注册表 HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run
非 Windows 平台空实现
"""

import sys
import os

if sys.platform == "win32":
    import winreg
else:
    winreg = None


def _is_frozen() -> bool:
    """检测是否运行在打包环境（PyInstaller 或 Nuitka 兼容）"""
    from phonemic.utils.paths import is_frozen
    return is_frozen()


def _get_exe_path() -> str:
    """获取开机自启的启动命令（带引号，以防路径含空格）。

    - 打包后：返回 exe 路径，如 "C:\\path\\PhoneMic.exe"
    - 开发环境：返回 .venv 中的 python 解释器 + "-m phonemic.PhoneMic"
      优先使用 pythonw.exe（无控制台窗口），避免开机时弹出黑窗。
      依赖 .venv 的 editable install（_editable_impl_phonemic.pth）使包可在任意工作目录导入。
    """
    exe = sys.executable
    if _is_frozen():
        # 打包后的 exe
        return f'"{exe}"'
    else:
        # 开发环境：优先使用同目录下的 pythonw.exe（无控制台窗口）
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        return f'"{exe}" -m phonemic.PhoneMic'


def is_auto_start_enabled() -> bool:
    """返回当前用户是否已启用开机自启动。

    注册表项存在时，进一步校验其中记录的可执行路径是否仍然存在，
    避免开发/打包环境切换或项目移动后误报为"已启用"。
    """
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ
        )
        value, _ = winreg.QueryValueEx(key, "PhoneMic")
        winreg.CloseKey(key)
        # value 形如: "C:\\path\\pythonw.exe" -m phonemic.PhoneMic --silent
        # 或: "C:\\path\\PhoneMic.exe" --silent
        # 提取首个带引号的路径并校验是否存在
        return _command_target_exists(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _command_target_exists(command: str) -> bool:
    """从注册表命令字符串中提取首个带引号的可执行路径，校验其是否存在。"""
    try:
        if '"' in command:
            path = command.split('"', 2)[1]
        else:
            path = command.split()[0]
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def set_auto_start_enabled(enabled: bool, silent: bool = False) -> None:
    """启用或禁用开机自启动。

    :param enabled: True=写入注册表启用自启，False=删除注册表项
    :param silent: 启用时是否以静默模式启动（追加 --silent 参数）。
                   仅当 enabled=True 时有效。
    """
    if sys.platform != "win32" or winreg is None:
        return

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        if enabled:
            # 写入或更新
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
            exe_path = _get_exe_path()
            # 根据静默偏好决定是否追加 --silent
            if silent:
                cmd = f'{exe_path} --silent'
            else:
                cmd = exe_path
            winreg.SetValueEx(key, "PhoneMic", 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
        else:
            # 删除
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
            winreg.DeleteValue(key, "PhoneMic")
            winreg.CloseKey(key)
    except FileNotFoundError:
        # 如果键不存在，忽略
        pass
    except Exception as e:
        # 记录日志但忽略
        print(f"Failed to set auto-start: {e}")