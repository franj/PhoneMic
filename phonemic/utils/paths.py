import json
import os
import sys
import builtins
from pathlib import Path


def is_frozen() -> bool:
    """检测当前是否运行在打包环境。

    兼容 PyInstaller 和 Nuitka：
    - PyInstaller 设置 sys.frozen = True
    - Nuitka 不设置 sys.frozen，而是注入 __compiled__ 到 builtins 命名空间

    参考: https://github.com/Nuitka/Nuitka/issues/216
    """
    return getattr(sys, 'frozen', False) or hasattr(builtins, '__compiled__')


def get_app_root() -> Path:
    """
    返回应用根目录（ phonemic 包的父目录）。
    假设目录结构：
    project_root/
        phonemic/
            resources/
            utils/
                paths.py
    """
    return Path(__file__).parent.parent.parent

def get_res_path(relative_path: str) -> str:
    """返回 resources 目录下某个相对路径的绝对路径"""
    full_path = get_app_root() / "phonemic/resources" / relative_path
    return str(full_path.resolve())

def _get_local_app_data() -> Path:
    """Windows: %LOCALAPPDATA%，其他平台: ~/.local/share 或 ~/.phonemic"""
    if sys.platform == 'win32':
        local_app_data = os.environ.get('LOCALAPPDATA')
        if not local_app_data:
            local_app_data = str(Path.home() / 'AppData' / 'Local')
        return Path(local_app_data)
    else:
        # Linux/macOS 使用 XDG 标准
        xdg_data_home = os.environ.get('XDG_DATA_HOME')
        if xdg_data_home:
            return Path(xdg_data_home)
        return Path.home() / '.local' / 'share'

def get_config_dir() -> Path:
    """返回配置目录（用于存储 settings.json, commands.json 等）"""
    config_dir = _get_local_app_data() / 'PhoneMic' / 'config'
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_exec_workdir() -> Path:
    """返回命令执行时的默认工作目录"""
    workdir = _get_local_app_data() / 'PhoneMic' / 'exec_workdir'
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir

def get_build_info():
    """
    读取构建信息，返回 (version, commit, date) 元组。
    如果读取失败，返回默认值。
    """
    # 方法1：从同目录下的 build_info.json 读取（打包后存在）
    info_file = get_app_root() / "build_info.json"
    if info_file.exists():
        try:
            with open(info_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("version", "0.0.0"), data.get("commit", "unknown"), data.get("date", "")
        except:
            pass
    
    # 方法2（可选）：开发环境从 pyproject.toml 读取
    try:
        import tomllib
        pyproject_path = get_app_root() / "pyproject.toml"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                version = data["project"]["version"]
                # 开发环境没有 commit 和 date，可留空或从 git 动态获取
                return version, "dev", ""
    except:
        pass
    
    # 最终 fallback
    return "0.0.0", "unknown", ""