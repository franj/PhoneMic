"""
测试 utils.startup 模块（开机自启动注册表操作）
"""

import sys
import pytest
from unittest.mock import MagicMock

# 导入要测试的模块
from phonemic.utils import startup


def test_is_auto_start_enabled_non_windows(mocker):
    """非 Windows 平台，返回 False"""
    mocker.patch('sys.platform', 'linux')
    assert startup.is_auto_start_enabled() is False


def test_is_auto_start_enabled_registry_exists(mocker):
    """模拟注册表存在且记录的可执行路径有效"""
    mock_winreg = MagicMock()
    mock_key = MagicMock()
    mock_winreg.OpenKey.return_value = mock_key
    # 返回一个带引号路径的命令串，并让路径校验通过
    mock_winreg.QueryValueEx.return_value = ('"C:\\PhoneMic.exe" --silent', 1)
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.KEY_READ = 0x20019

    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)
    mocker.patch('phonemic.utils.startup.os.path.exists', return_value=True)

    assert startup.is_auto_start_enabled() is True


def test_is_auto_start_enabled_target_missing(mocker):
    """注册表项存在但记录的可执行路径已失效（项目移动/环境切换），返回 False"""
    mock_winreg = MagicMock()
    mock_key = MagicMock()
    mock_winreg.OpenKey.return_value = mock_key
    mock_winreg.QueryValueEx.return_value = ('"D:\\old\\pythonw.exe" -m phonemic.PhoneMic --silent', 1)
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.KEY_READ = 0x20019

    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)
    mocker.patch('phonemic.utils.startup.os.path.exists', return_value=False)

    assert startup.is_auto_start_enabled() is False


def test_is_auto_start_enabled_registry_missing(mocker):
    """注册表项不存在，返回 False"""
    mock_winreg = MagicMock()
    mock_winreg.OpenKey.side_effect = FileNotFoundError
    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)

    assert startup.is_auto_start_enabled() is False


def test_set_auto_start_enabled_enable(mocker):
    """启用开机自启动（默认非静默），写入注册表不带 --silent"""
    mock_winreg = MagicMock()
    mock_key = MagicMock()
    # 第一次 OpenKey 失败（不存在），然后 CreateKey 创建
    mock_winreg.OpenKey.side_effect = [FileNotFoundError, mock_key]
    mock_winreg.CreateKey.return_value = mock_key
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.KEY_READ = 0x20019
    mock_winreg.KEY_WRITE = 0x20006
    mock_winreg.REG_SZ = 1

    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)
    # 让 _get_exe_path 返回带引号的路径（与真实行为一致）
    mocker.patch('phonemic.utils.startup._get_exe_path', return_value='"C:\\PhoneMic.exe"')

    startup.set_auto_start_enabled(True)

    # 验证 CreateKey 被调用
    mock_winreg.CreateKey.assert_called_once_with(
        mock_winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
    )
    # 验证 SetValueEx 被调用，值为带引号的路径（无 --silent）
    mock_winreg.SetValueEx.assert_called_once_with(
        mock_key, "PhoneMic", 0, mock_winreg.REG_SZ, '"C:\\PhoneMic.exe"'
    )


def test_set_auto_start_enabled_enable_silent(mocker):
    """启用开机自启动且 silent=True，注册表值追加 --silent"""
    mock_winreg = MagicMock()
    mock_key = MagicMock()
    mock_winreg.OpenKey.side_effect = [FileNotFoundError, mock_key]
    mock_winreg.CreateKey.return_value = mock_key
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.KEY_READ = 0x20019
    mock_winreg.KEY_WRITE = 0x20006
    mock_winreg.REG_SZ = 1

    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)
    mocker.patch('phonemic.utils.startup._get_exe_path', return_value='"C:\\PhoneMic.exe"')

    startup.set_auto_start_enabled(True, silent=True)

    mock_winreg.SetValueEx.assert_called_once_with(
        mock_key, "PhoneMic", 0, mock_winreg.REG_SZ, '"C:\\PhoneMic.exe" --silent'
    )


def test_set_auto_start_enabled_disable(mocker):
    """禁用开机自启动，删除注册表项"""
    mock_winreg = MagicMock()
    mock_key = MagicMock()
    mock_winreg.OpenKey.return_value = mock_key
    mock_winreg.HKEY_CURRENT_USER = 0x80000001
    mock_winreg.KEY_WRITE = 0x20006

    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)

    startup.set_auto_start_enabled(False)

    # 验证 DeleteValue 被调用
    mock_winreg.DeleteValue.assert_called_once_with(mock_key, "PhoneMic")


def test_set_auto_start_enabled_disable_key_not_found(mocker):
    """禁用时如果键不存在，不应出错"""
    mock_winreg = MagicMock()
    mock_winreg.OpenKey.side_effect = FileNotFoundError
    mocker.patch('phonemic.utils.startup.winreg', mock_winreg)

    # 不应抛出异常
    startup.set_auto_start_enabled(False)  # 无断言，只要不崩溃即通过


# ===================== _get_exe_path 测试 =====================

def test_get_exe_path_frozen(mocker):
    """打包环境（Nuitka/PyInstaller）：返回带引号的 exe 路径"""
    mocker.patch('sys.executable', 'C:\\Program Files\\PhoneMic\\PhoneMic.exe')
    mocker.patch('phonemic.utils.startup._is_frozen', return_value=True)
    result = startup._get_exe_path()
    assert result == '"C:\\Program Files\\PhoneMic\\PhoneMic.exe"'


def test_get_exe_path_dev_uses_pythonw(mocker, tmp_path):
    """开发环境：优先使用同目录 pythonw.exe，并附加 -m phonemic.PhoneMic"""
    venv_scripts = tmp_path / "Scripts"
    venv_scripts.mkdir()
    python_exe = venv_scripts / "python.exe"
    pythonw_exe = venv_scripts / "pythonw.exe"
    python_exe.write_text("")
    pythonw_exe.write_text("")

    mocker.patch('sys.executable', str(python_exe))
    mocker.patch('phonemic.utils.startup._is_frozen', return_value=False)

    result = startup._get_exe_path()
    assert '-m phonemic.PhoneMic' in result
    assert 'pythonw.exe' in result
    assert result.startswith('"')  # 路径带引号


def test_get_exe_path_dev_fallback_to_python(mocker, tmp_path):
    """开发环境：无 pythonw.exe 时回退到 python.exe"""
    venv_scripts = tmp_path / "Scripts"
    venv_scripts.mkdir()
    python_exe = venv_scripts / "python.exe"
    python_exe.write_text("")

    mocker.patch('sys.executable', str(python_exe))
    mocker.patch('phonemic.utils.startup._is_frozen', return_value=False)

    result = startup._get_exe_path()
    assert '-m phonemic.PhoneMic' in result
    assert 'python.exe' in result
    assert 'pythonw.exe' not in result