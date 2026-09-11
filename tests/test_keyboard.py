# tests/test_keyboard.py
"""
单元测试：键盘上屏模块 (flash_insert)
验证剪贴板操作、模拟按键及异常恢复行为。
"""

import pytest
import pyperclip
import pyautogui
from phonemic.gui import keyboard
from phonemic.gui.keyboard import flash_insert


@pytest.fixture(autouse=True)
def force_paste_mode():
    """本文件的 flash_insert 用例针对剪贴板路径，固定上屏方式避免受配置/前台窗口影响"""
    keyboard.set_input_mode_override("paste")
    yield
    keyboard.set_input_mode_override(None)


class TestFlashInsert:
    """测试 flash_insert 函数"""

    def test_flash_insert_normal(self, mocker):
        """正常流程：剪贴板正确写入+恢复，hotkey 被调用一次"""
        # Mock 外部依赖
        mock_paste = mocker.patch.object(pyperclip, 'paste', return_value="original text")
        mock_copy = mocker.patch.object(pyperclip, 'copy')
        mock_hotkey = mocker.patch.object(pyautogui, 'hotkey')

        # 执行
        flash_insert("hello world")

        # 验证
        # 1. 保存原剪贴板内容
        mock_paste.assert_called_once()
        # 2. 写入新文本
        mock_copy.assert_any_call("hello world")
        # 3. 模拟 Ctrl+V
        mock_hotkey.assert_called_once_with('ctrl', 'v')
        # 4. 恢复原剪贴板内容（第二次调用 copy）
        assert mock_copy.call_count == 2
        mock_copy.assert_called_with("original text")

    def test_flash_insert_empty_text(self, mocker):
        """空文本：提前返回，不操作剪贴板和键盘"""
        mock_paste = mocker.patch.object(pyperclip, 'paste')
        mock_copy = mocker.patch.object(pyperclip, 'copy')
        mock_hotkey = mocker.patch.object(pyautogui, 'hotkey')

        flash_insert("")

        mock_paste.assert_not_called()
        mock_copy.assert_not_called()
        mock_hotkey.assert_not_called()

    def test_flash_insert_clipboard_failure(self, mocker):
        """模拟 pyperclip.copy 抛出异常 -> 应传播异常，但恢复原剪贴板仍会尝试"""
        original_text = "original"
        mock_paste = mocker.patch.object(pyperclip, 'paste', return_value=original_text)
        # 第一次 copy（写入新文本）失败
        mock_copy = mocker.patch.object(pyperclip, 'copy', side_effect=pyperclip.PyperclipException("copy failed"))

        with pytest.raises(RuntimeError, match="Clipboard operation failed: copy failed"):
            flash_insert("new text")

        # 验证：粘贴原始内容被调用了一次
        mock_paste.assert_called_once()
        # 尝试写入新文本时失败，但恢复操作仍会执行（第二次 copy）
        # 注意：由于 side_effect 在第一次调用时抛出异常，第二次 copy（恢复）永远不会执行
        # 因为异常已经抛出，finally 块中的恢复代码不会执行？—— 不对，原代码中 finally 块始终执行
        # 但这里 side_effect 每次调用都会抛出异常，所以第二次 copy 也会失败，但不会影响异常传播
        # 实际原代码中，如果第一次 copy 失败，original_clipboard 已保存，finally 会尝试恢复
        # 但恢复的 copy 也会失败（因为 side_effect 持续生效），导致异常被抛出（原异常优先）
        # 检查：finally 块中的异常会被 logging.critical 捕获，但不会重新抛出，所以原异常正常传播
        # 我们只需验证原异常被抛出即可。
        # 验证第二次 copy 也被尝试（恢复）
        assert mock_copy.call_count == 2  # 第一次写入失败，第二次恢复也失败（side_effect 继续）
        # 但原异常已经是 RuntimeError，测试通过

    def test_flash_insert_restore_failure(self, mocker, caplog):
        """模拟恢复剪贴板失败（写入 original 时异常），应记录 critical 日志，不抛出异常"""
        import logging
        original_text = "original"
        mock_paste = mocker.patch.object(pyperclip, 'paste', return_value=original_text)
        # 第一次 copy（写入新文本）成功，第二次 copy（恢复）失败
        def copy_side_effect(text):
            if text == "new text":
                return  # 成功
            else:
                raise pyperclip.PyperclipException("restore failed")

        mock_copy = mocker.patch.object(pyperclip, 'copy', side_effect=copy_side_effect)
        mock_hotkey = mocker.patch.object(pyautogui, 'hotkey')

        # 执行：不应抛出异常
        flash_insert("new text")

        # 验证调用次数
        mock_paste.assert_called_once()
        assert mock_copy.call_count == 2  # 第一次成功写入 new text，第二次恢复失败
        mock_hotkey.assert_called_once_with('ctrl', 'v')

        # 检查日志：应包含 critical 级别的恢复失败记录
        assert any(rec.levelno == logging.CRITICAL and "Failed to restore original clipboard" in rec.message
                   for rec in caplog.records)

    def test_flash_insert_unicode_text(self, mocker):
        """Unicode/emoji 文本应正确处理"""
        mock_paste = mocker.patch.object(pyperclip, 'paste', return_value="orig")
        mock_copy = mocker.patch.object(pyperclip, 'copy')
        mock_hotkey = mocker.patch.object(pyautogui, 'hotkey')

        emoji_text = "Hello 😊 世界"
        flash_insert(emoji_text)

        mock_copy.assert_any_call(emoji_text)
        mock_hotkey.assert_called_once()
        # 恢复原内容
        mock_copy.assert_called_with("orig")

import pytest
from unittest.mock import patch, MagicMock
import pyautogui
import time

from phonemic.gui.keyboard import validate_key_combination, send_keys, flash_insert

# ---------- validate_key_combination ----------
def test_validate_valid_combinations():
    # 合法组合
    assert validate_key_combination("ctrl+c") == (True, "")
    assert validate_key_combination("shift+alt+a") == (True, "")
    assert validate_key_combination("enter") == (True, "")
    assert validate_key_combination("ctrl+shift+win+alt") == (True, "")  # 多修饰键
    assert validate_key_combination("space") == (True, "")


def test_validate_invalid_empty():
    assert validate_key_combination("") == (False, "按键字符串不能为空")
    assert validate_key_combination("   ") == (False, "按键字符串不能为空")


def test_validate_unknown_key():
    assert validate_key_combination("foo")[0] is False
    assert "未知键名: 'foo'" in validate_key_combination("foo")[1]
    # 组合中含未知键
    assert validate_key_combination("ctrl+foo")[0] is False


def test_validate_case_insensitive():
    # 应转为小写校验，大写也合法
    assert validate_key_combination("CTRL+C") == (True, "")


# ---------- send_keys ----------
@patch("phonemic.gui.keyboard.pyautogui.hotkey")
@patch("phonemic.gui.keyboard.time.sleep")
def test_send_keys_valid(mock_sleep, mock_hotkey):
    send_keys("ctrl+c")
    # _pause=False：每次调用都不许带 pyautogui 的 0.1s 停顿
    mock_hotkey.assert_called_once_with("ctrl", "c", _pause=False)
    # 单个组合后面不再停顿（连发时这就是白等）
    mock_sleep.assert_not_called()


@patch("phonemic.gui.keyboard.pyautogui.hotkey")
def test_send_keys_invalid_logs_error(mock_hotkey, caplog):
    with caplog.at_level("ERROR"):
        send_keys("invalid+key")
    mock_hotkey.assert_not_called()
    assert "非法" in caplog.text


@patch("phonemic.gui.keyboard.pyautogui.hotkey")
def test_send_keys_exception_handled(mock_hotkey, caplog):
    mock_hotkey.side_effect = Exception("模拟异常")
    with caplog.at_level("ERROR"):
        send_keys("ctrl+c")
    assert "模拟按键失败" in caplog.text


# ---------- flash_insert (已有的函数，确保兼容) ----------
@patch("phonemic.gui.keyboard.pyperclip")
@patch("phonemic.gui.keyboard.pyautogui")
def test_flash_insert_restores_clipboard(mock_autogui, mock_pyperclip):
    # 假设 flash_insert 实现：保存剪贴板 -> 写入新文本 -> ctrl+v -> 恢复剪贴板
    mock_pyperclip.paste.return_value = "original"
    mock_pyperclip.copy.side_effect = lambda x: None

    from phonemic.gui.keyboard import flash_insert
    flash_insert("new text")

    mock_pyperclip.paste.assert_called()
    mock_pyperclip.copy.assert_any_call("new text")
    mock_autogui.hotkey.assert_called_with("ctrl", "v")
    # 恢复原剪贴板
    mock_pyperclip.copy.assert_called_with("original")

# ---------- 新增测试：按键序列 (validate_key_sequence & send_keys 序列) ----------
from phonemic.gui.keyboard import validate_key_sequence

def test_validate_key_sequence_valid():
    """合法的按键序列"""
    assert validate_key_sequence("ctrl+a, delete") == (True, "")
    assert validate_key_sequence("ctrl+c, ctrl+v") == (True, "")
    assert validate_key_sequence("enter, tab, space") == (True, "")
    assert validate_key_sequence("ctrl+shift+esc, win+d") == (True, "")
    # 单个组合（不带逗号）也应视为合法序列
    assert validate_key_sequence("ctrl+c") == (True, "")

def test_validate_key_sequence_too_long():
    """超过10个组合应报错"""
    long_seq = ",".join(["ctrl+a"] * 11)  # 11个组合
    ok, err = validate_key_sequence(long_seq)
    assert ok is False
    assert "最多支持10个组合" in err

def test_validate_key_sequence_invalid_combo():
    """序列中包含非法组合"""
    ok, err = validate_key_sequence("ctrl+c, invalid")
    assert ok is False
    assert "组合 'invalid' 无效" in err

def test_validate_key_sequence_empty():
    ok, err = validate_key_sequence("")
    assert ok is False
    assert "不能为空" in err
    ok, err = validate_key_sequence("   ")
    assert ok is False

def test_validate_key_sequence_mixed_spaces():
    """序列中允许有空格（自动strip）"""
    ok, err = validate_key_sequence(" ctrl+a , delete ")
    assert ok is True

@patch("phonemic.gui.keyboard.pyautogui.hotkey")
@patch("phonemic.gui.keyboard.time.sleep")
def test_send_keys_sequence_multiple_combos(mock_sleep, mock_hotkey):
    """测试序列：应依次调用 hotkey，每个组合后短暂延迟"""
    send_keys("ctrl+a, delete, enter")
    # 预期调用三次 hotkey
    expected_calls = [
        ('ctrl', 'a'),
        ('delete',),
        ('enter',)
    ]
    assert mock_hotkey.call_count == 3
    for i, call in enumerate(mock_hotkey.call_args_list):
        # call[0] 是位置参数元组，例如 ('ctrl', 'a')
        assert call[0] == expected_calls[i]
        assert call[1] == {"_pause": False}
    # 停顿只在组合之间：3 个组合 → 2 次
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(0.05)

@patch("phonemic.gui.keyboard.pyautogui.hotkey")
@patch("phonemic.gui.keyboard.time.sleep")
def test_send_keys_sequence_stops_on_error(mock_sleep, mock_hotkey):
    """如果序列中某个组合执行失败，应停止后续组合"""
    # 让第一次调用成功，第二次失败，第三次不会被调用
    mock_hotkey.side_effect = [None, Exception("模拟失败"), None]
    with patch("phonemic.gui.keyboard.logger") as mock_logger:
        send_keys("ctrl+a, delete, enter")
        # 应该只执行前两个组合（第二个失败后停止，第三个未执行）
        assert mock_hotkey.call_count == 2
        # 验证日志记录了错误
        mock_logger.exception.assert_called_once()
    # sleep 应该只被调用一次（第一次成功后的延迟，第二次失败后没有 sleep 就 break）
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_with(0.05)

@patch("phonemic.gui.keyboard.pyautogui.hotkey")
@patch("phonemic.gui.keyboard.time.sleep")
def test_send_keys_sequence_single_combo(mock_sleep, mock_hotkey):
    """向后兼容：单个组合（不带逗号）仍然正常工作"""
    send_keys("ctrl+c")
    mock_hotkey.assert_called_once_with("ctrl", "c", _pause=False)
    mock_sleep.assert_not_called()


@patch("phonemic.gui.keyboard.pyautogui.hotkey")
@patch("phonemic.gui.keyboard.time.sleep")
def test_send_keys_repeat_carries_no_artificial_delay(mock_sleep, mock_hotkey):
    """按住连发（手机端 50ms 一帧）不得有任何人为停顿。

    有停顿就会出现「PC 消费速度 < 手机发送速度」→ 事件积压 → 按住删得慢、
    松手后还在继续删。注意 stop 之后 time.sleep(0.05) 也不该被调到。
    """
    for _ in range(20):
        send_keys("backspace")

    assert mock_hotkey.call_count == 20
    assert all(call[1] == {"_pause": False} for call in mock_hotkey.call_args_list)
    mock_sleep.assert_not_called()

@patch("phonemic.gui.keyboard.pyautogui.hotkey")
def test_send_keys_invalid_sequence_logs_error(mock_hotkey, caplog):
    """非法序列不应调用 hotkey，并记录错误"""
    with caplog.at_level("ERROR"):
        send_keys("invalid, ctrl+a")  # 第一个组合非法
    mock_hotkey.assert_not_called()
    assert "按键序列非法" in caplog.text

# 注意：原有的 validate_key_combination 仍然可正常工作，无需额外测试

# ---------- 上屏方式分发 ----------
from phonemic.gui.keyboard import get_input_mode, set_input_mode_override
from phonemic.gui.text_input import SendTextError


@pytest.fixture
def restore_mode():
    yield
    set_input_mode_override(None)


def test_get_input_mode_override(restore_mode):
    set_input_mode_override("type")
    assert get_input_mode() == "type"
    set_input_mode_override(None)
    # 取消覆盖后回落到配置/默认值，取值必须合法
    assert get_input_mode() in ("paste", "type")


def test_auto_mode_no_longer_accepted(restore_mode):
    """auto 模式已移除，不应再被当作合法取值"""
    with pytest.raises(ValueError):
        set_input_mode_override("auto")


def test_set_input_mode_override_rejects_unknown(restore_mode):
    with pytest.raises(ValueError):
        set_input_mode_override("nonsense")


@patch("phonemic.gui.keyboard.text_input.send_text")
@patch("phonemic.gui.keyboard.flash_insert_via_clipboard")
def test_flash_insert_type_mode_uses_send_text(mock_clip, mock_send_text, restore_mode):
    """type 模式走模拟键盘输入，完全不碰剪贴板"""
    set_input_mode_override("type")
    flash_insert("hello")
    mock_send_text.assert_called_once_with("hello")
    mock_clip.assert_not_called()


@patch("phonemic.gui.keyboard.text_input.send_text")
@patch("phonemic.gui.keyboard.flash_insert_via_clipboard")
def test_flash_insert_paste_mode_uses_clipboard(mock_clip, mock_send_text, restore_mode):
    set_input_mode_override("paste")
    flash_insert("hello")
    mock_clip.assert_called_once_with("hello")
    mock_send_text.assert_not_called()


@patch("phonemic.gui.keyboard.text_input.send_text",
       side_effect=SendTextError("UIPI 拦截", partial=False))
@patch("phonemic.gui.keyboard.flash_insert_via_clipboard")
def test_flash_insert_falls_back_when_nothing_injected(mock_clip, mock_send_text, caplog, restore_mode):
    """一个字符都没注入时回退到剪贴板粘贴，不向上抛异常"""
    set_input_mode_override("type")
    with caplog.at_level("WARNING"):
        flash_insert("hello")
    mock_send_text.assert_called_once_with("hello")
    mock_clip.assert_called_once_with("hello")
    assert "回退到剪贴板粘贴" in caplog.text


@patch("phonemic.gui.keyboard.text_input.send_text",
       side_effect=SendTextError("中途中断", partial=True))
@patch("phonemic.gui.keyboard.flash_insert_via_clipboard")
def test_flash_insert_no_fallback_after_partial_injection(mock_clip, mock_send_text, caplog, restore_mode):
    """已注入部分字符后不得回退粘贴，否则这部分文字会重复上屏"""
    set_input_mode_override("type")
    with caplog.at_level("ERROR"):
        with pytest.raises(SendTextError):
            flash_insert("hello")
    mock_clip.assert_not_called()
    assert "不回退以免重复" in caplog.text


@patch("phonemic.gui.keyboard.text_input.send_text")
@patch("phonemic.gui.keyboard.flash_insert_via_clipboard")
def test_flash_insert_empty_text_no_dispatch(mock_clip, mock_send_text, restore_mode):
    set_input_mode_override("type")
    flash_insert("")
    mock_send_text.assert_not_called()
    mock_clip.assert_not_called()
