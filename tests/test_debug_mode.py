"""
phonemic.utils.debug 的开关语义。

三条产品行为在这里钉死：
- 默认（不设环境变量、不带 --debug）**不是**调试模式；
- PHONEMIC_DEBUG 认「明确开启」的写法，写错/写 0 都算关闭；
- 命令行（set_debug_mode）优先于环境变量，reset 后回落到环境变量。
"""

import pytest

from phonemic.utils import debug as debug_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「干净环境 + 无命令行覆盖」开始。"""
    monkeypatch.delenv(debug_mod.DEBUG_ENV, raising=False)
    debug_mod.reset_debug_mode()
    yield
    debug_mod.reset_debug_mode()


def test_default_off():
    """直接运行不进调试模式：这是本次改动的核心诉求。"""
    assert debug_mod.is_debug_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " y ", "t"])
def test_env_truthy(monkeypatch, value):
    monkeypatch.setenv(debug_mod.DEBUG_ENV, value)
    assert debug_mod.is_debug_mode() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "n", "maybe", "2"])
def test_env_falsy(monkeypatch, value):
    """未知取值一律按关闭处理：宁可不进调试，也不悄悄多开诊断入口。"""
    monkeypatch.setenv(debug_mod.DEBUG_ENV, value)
    assert debug_mod.is_debug_mode() is False


def test_cli_override_beats_env(monkeypatch):
    """命令行显式表态压过环境里遗留的变量。"""
    monkeypatch.setenv(debug_mod.DEBUG_ENV, "1")
    debug_mod.set_debug_mode(False)
    assert debug_mod.is_debug_mode() is False


def test_reset_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(debug_mod.DEBUG_ENV, "1")
    debug_mod.set_debug_mode(False)
    debug_mod.reset_debug_mode()
    assert debug_mod.is_debug_mode() is True
