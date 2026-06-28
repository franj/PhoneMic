"""
测试 phonemic.utils.paths.is_frozen() 的打包环境检测
覆盖 PyInstaller (sys.frozen) 和 Nuitka (builtins.__compiled__ / globals().__compiled__) 多种场景
"""

import sys
import builtins
import pytest
from phonemic.utils.paths import is_frozen


def _clean_frozen_state():
    """清理所有打包检测相关的状态，确保测试隔离"""
    # 清理 sys.frozen
    if hasattr(sys, 'frozen'):
        del sys.frozen
    # 清理 builtins.__compiled__
    if hasattr(builtins, '__compiled__'):
        del builtins.__compiled__
    # 清理当前模块 globals 中的 __compiled__
    g = globals()
    if '__compiled__' in g:
        del g['__compiled__']


def test_is_frozen_dev_environment():
    """开发环境：既无 sys.frozen 也无 __compiled__（任何位置），返回 False"""
    _clean_frozen_state()
    assert is_frozen() is False


def test_is_frozen_pyinstaller(monkeypatch):
    """PyInstaller：sys.frozen=True 时返回 True"""
    _clean_frozen_state()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert is_frozen() is True


def test_is_frozen_nuitka_in_builtins(monkeypatch):
    """Nuitka：注入 __compiled__ 到 builtins 时返回 True"""
    _clean_frozen_state()
    monkeypatch.setattr(builtins, '__compiled__', object(), raising=False)
    assert is_frozen() is True


def test_is_frozen_nuitka_in_globals(monkeypatch):
    """Nuitka：__compiled__ 在 paths 模块自身 globals 中时应返回 True

    Nuitka 将 __compiled__ 作为 compile-time attribute 注入到每个模块的 globals。
    此处通过 import 后直接操作目标模块的 __dict__ 来模拟这一行为。
    """
    _clean_frozen_state()
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.delattr(builtins, '__compiled__', raising=False)
    # 直接在 paths 模块的 globals 中注入 __compiled__（模拟 Nuitka 行为）
    import phonemic.utils.paths as paths_module
    paths_module.__dict__['__compiled__'] = object()
    try:
        assert is_frozen() is True
    finally:
        if '__compiled__' in paths_module.__dict__:
            del paths_module.__dict__['__compiled__']


def test_is_frozen_nuitka_version_info_shape(monkeypatch):
    """Nuitka 的 __compiled__ 实际为 namedtuple，含版本信息，应同样被识别"""
    from collections import namedtuple
    NuitkaVersion = namedtuple(
        'nuitka_version',
        'major minor micro releaselevel standalone onefile'
    )
    fake_compiled = NuitkaVersion(2, 7, 0, 'release', True, False)
    _clean_frozen_state()
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(builtins, '__compiled__', fake_compiled, raising=False)
    assert is_frozen() is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
