"""
测试 phonemic.utils.paths.is_frozen() 的打包环境检测
覆盖 PyInstaller (sys.frozen) 和 Nuitka (builtins.__compiled__) 两种场景
"""

import sys
import builtins
import pytest
from phonemic.utils.paths import is_frozen


def test_is_frozen_dev_environment():
    """开发环境：既无 sys.frozen 也无 __compiled__，返回 False"""
    original_frozen = getattr(sys, 'frozen', None)
    original_compiled = getattr(builtins, '__compiled__', None)
    if hasattr(sys, 'frozen'):
        del sys.frozen
    if hasattr(builtins, '__compiled__'):
        del builtins.__compiled__
    try:
        assert is_frozen() is False
    finally:
        if original_frozen is not None:
            sys.frozen = original_frozen
        if original_compiled is not None:
            builtins.__compiled__ = original_compiled


def test_is_frozen_pyinstaller(monkeypatch):
    """PyInstaller：sys.frozen=True 时返回 True"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.delattr(builtins, '__compiled__', raising=False)
    assert is_frozen() is True


def test_is_frozen_nuitka(monkeypatch):
    """Nuitka：注入 __compiled__ 到 builtins 时返回 True"""
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(builtins, '__compiled__', object(), raising=False)
    assert is_frozen() is True


def test_is_frozen_nuitka_version_info_shape(monkeypatch):
    """Nuitka 的 __compiled__ 实际为 namedtuple，含版本信息，应同样被识别"""
    from collections import namedtuple
    NuitkaVersion = namedtuple(
        'nuitka_version',
        'major minor micro releaselevel standalone onefile'
    )
    fake_compiled = NuitkaVersion(2, 7, 0, 'release', True, False)
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(builtins, '__compiled__', fake_compiled, raising=False)
    assert is_frozen() is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
