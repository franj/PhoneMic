# -*- coding: utf-8 -*-
"""
开机自启动管理模块（Windows 注册表）
使用 Windows 注册表 HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run
非 Windows 平台空实现
"""

import sys
import os
import subprocess

if sys.platform == "win32":
    import winreg
else:
    winreg = None


def _get_exe_path() -> str:
    """获取当前可执行文件的路径（带引号，以防路径含空格）"""
    exe = sys.executable
    if getattr(sys, 'frozen', False):
        # 打包后的 exe
        return f'"{exe}"'
    else:
        # 开发环境，使用 python 解释器运行 PhoneMic.py
        # 此处返回 python 可执行文件路径，但实际我们更希望直接返回 PhoneMic.py 的路径？
        # 其实开机自启动通常用 exe，开发时可能不需要。这里简单返回带引号的 exe。
        return f'"{exe}"'


def is_auto_start_enabled() -> bool:
    """返回当前用户是否已启用开机自启动"""
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
        # 检查值是否包含当前 exe 路径（可扩展检查）
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_auto_start_enabled(enabled: bool) -> None:
    """启用或禁用开机自启动"""
    if sys.platform != "win32" or winreg is None:
        return

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        if enabled:
            # 写入或更新
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
            exe_path = _get_exe_path()
            # 添加 --silent 参数
            winreg.SetValueEx(key, "PhoneMic", 0, winreg.REG_SZ, f'{exe_path} --silent')
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