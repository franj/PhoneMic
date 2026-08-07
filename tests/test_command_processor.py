import pytest
import subprocess
from unittest.mock import patch, MagicMock
import sys
import os
import logging

# 导入实际代码中的类
from phonemic.utils.command_processor import match_command, execute_command
from phonemic.utils.commands_manager import VoiceCommand

# ---------- 测试数据 ----------
@pytest.fixture
def sample_commands():
    return [
        VoiceCommand(
            id="1",
            name="exact test",
            matchType="exact",
            matchPattern="hello",
            actionType="key",
            actionParams="enter",
            enabled=True,
        ),
        VoiceCommand(
            id="2",
            name="prefix test",
            matchType="prefix",
            matchPattern="calc ",
            actionType="exec",
            actionParams="python -c \"print('{content}')\"",
            enabled=True,
        ),
        VoiceCommand(
            id="3",
            name="disabled command",
            matchType="exact",
            matchPattern="disabled",
            actionType="key",
            actionParams="a",
            enabled=False,
        ),
    ]


# ---------- match_command 测试 ----------
def test_match_command_exact(sample_commands):
    result = match_command("hello", sample_commands)
    assert result is not None
    cmd, prefix, content = result
    assert cmd.id == "1"
    assert prefix == "hello"
    assert content == ""


def test_match_command_prefix(sample_commands):
    result = match_command("calc 2+2", sample_commands)
    assert result is not None
    cmd, prefix, content = result
    assert cmd.id == "2"
    assert prefix == "calc "
    assert content == "2+2"


def test_match_command_disabled(sample_commands):
    # 禁用命令不应被匹配
    result = match_command("disabled", sample_commands)
    assert result is None


def test_match_command_no_match(sample_commands):
    result = match_command("unknown", sample_commands)
    assert result is None


def test_match_command_order_respects_list_order():
    # 两个命令都能匹配时，应返回第一个
    commands = [
        VoiceCommand(id="first", name="", matchType="prefix", matchPattern="a",
                     actionType="key", actionParams="", enabled=True),
        VoiceCommand(id="second", name="", matchType="prefix", matchPattern="ab",
                     actionType="key", actionParams="", enabled=True),
    ]
    result = match_command("abc", commands)
    assert result is not None
    assert result[0].id == "first"


def test_match_command_empty_commands():
    assert match_command("anything", []) is None


# ---------- execute_command 测试：key 类型 ----------
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_action(mock_sleep, mock_send_keys):
    """纯按键组合（向后兼容）"""
    cmd = VoiceCommand(
        id="k1", name="", matchType="exact", matchPattern="",
        actionType="key", actionParams="ctrl+c", enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_send_keys.assert_called_once_with("ctrl+c")


@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_action_with_sequence(mock_sleep, mock_send_keys):
    """逗号分隔的按键序列：解析后逐段调用 send_keys"""
    cmd = VoiceCommand(
        id="seq1", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams="ctrl+a, delete",
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    # 应调用两次 send_keys，分别传入每个组合
    assert mock_send_keys.call_count == 2
    mock_send_keys.assert_any_call("ctrl+a")
    mock_send_keys.assert_any_call("delete")


@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_action_single_combo(mock_sleep, mock_send_keys):
    """向后兼容：单个组合不带逗号仍然正常工作"""
    cmd = VoiceCommand(
        id="seq2", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams="enter",
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_send_keys.assert_called_once_with("enter")


# ---------- execute_command 测试：key 类型混合粘贴文本 ----------
@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_with_text_segment(mock_sleep, mock_send_keys, mock_flash):
    """混合：粘贴文本 + 按键"""
    cmd = VoiceCommand(
        id="mix1", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams='"hello", enter',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_flash.assert_called_once_with("hello")
    mock_send_keys.assert_called_once_with("enter")


@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_quotes_pair(mock_sleep, mock_send_keys, mock_flash):
    """粘贴双引号对 + 左移光标：\"\"\", left"""
    cmd = VoiceCommand(
        id="mix2", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams=r'"\"\"", left',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_flash.assert_called_once_with('""')
    mock_send_keys.assert_called_once_with("left")


@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_text_with_content(mock_sleep, mock_send_keys, mock_flash):
    """粘贴文本中的 {content} 被替换"""
    cmd = VoiceCommand(
        id="mix3", name="", matchType="prefix", matchPattern="记录 ",
        actionType="key",
        actionParams='"【{content}】", enter',
        enabled=True
    )
    execute_command(cmd, all_text="记录 开心", prefix="记录 ", content="开心")
    mock_flash.assert_called_once_with("【开心】")
    mock_send_keys.assert_called_once_with("enter")


@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_text_with_time_date(mock_sleep, mock_send_keys, mock_flash):
    """粘贴文本中的 {time} {date} 被替换"""
    cmd = VoiceCommand(
        id="mix4", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams='"{date} {time}", enter',
        enabled=True
    )
    with patch("phonemic.utils.input_parser.datetime") as mock_dt:
        from datetime import datetime
        mock_dt.now.return_value = datetime(2026, 8, 7, 14, 30, 0)
        execute_command(cmd, all_text="", prefix="", content="")
    mock_flash.assert_called_once_with("2026-08-07 14:30:00")


@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_pure_text(mock_sleep, mock_send_keys, mock_flash):
    """纯粘贴文本（无按键）"""
    cmd = VoiceCommand(
        id="mix5", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams='"你好世界"',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_flash.assert_called_once_with("你好世界")
    mock_send_keys.assert_not_called()


@patch("phonemic.utils.command_processor.flash_insert")
@patch("phonemic.utils.command_processor.send_keys")
@patch("phonemic.utils.command_processor.time.sleep")
def test_execute_command_key_complex_mixed(mock_sleep, mock_send_keys, mock_flash):
    """复杂混合序列：全选+粘贴文本+粘贴引号对+左移+回车"""
    cmd = VoiceCommand(
        id="mix6", name="", matchType="exact", matchPattern="",
        actionType="key",
        actionParams=r'ctrl+a, "替换内容", ctrl+v, "\"\"", left, enter',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    # send_keys 被调用 4 次
    assert mock_send_keys.call_count == 4
    mock_send_keys.assert_any_call("ctrl+a")
    mock_send_keys.assert_any_call("ctrl+v")
    mock_send_keys.assert_any_call("left")
    mock_send_keys.assert_any_call("enter")
    # flash_insert 被调用 2 次
    assert mock_flash.call_count == 2
    mock_flash.assert_any_call("替换内容")
    mock_flash.assert_any_call('""')


# ---------- execute_command 测试：exec 类型 ----------
@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_exec_action(mock_popen):
    cmd = VoiceCommand(
        id="e1", name="", matchType="prefix", matchPattern="",
        actionType="exec", actionParams="echo {content}", enabled=True
    )
    execute_command(cmd, all_text="test", prefix="", content="hello world")
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    assert kwargs.get("shell") is False
    assert args[0] == ['echo', 'hello world']


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_complex_params_with_quotes(mock_popen):
    """带引号的 exec 参数：pyparsing 正确分词，不再有 shlex 歧义"""
    cmd = VoiceCommand(
        id="e2", name="", matchType="prefix", matchPattern="",
        actionType="exec", actionParams='python -c "print({content})"', enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="2+2")
    mock_popen.assert_called_once()
    args, _ = mock_popen.call_args
    # pyparsing 正确去除了引号
    assert args[0] == ['python', '-c', 'print(2+2)']


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_safe_no_injection(mock_popen):
    """验证先拆分再替换阻止注入"""
    cmd = VoiceCommand(
        id="e3", name="", matchType="prefix", matchPattern="",
        actionType="exec", actionParams="echo {content}", enabled=True
    )
    malicious_content = "hello && rm -rf /"
    execute_command(cmd, all_text="", prefix="", content=malicious_content)
    args, _ = mock_popen.call_args
    # 应该整个作为参数，不被拆分成两个命令
    assert args[0] == ['echo', 'hello && rm -rf /']


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_empty_tokens(mock_popen):
    cmd = VoiceCommand(
        id="e4", name="", matchType="prefix", matchPattern="",
        actionType="exec", actionParams="   ", enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_popen.assert_not_called()


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_unknown_placeholder_preserved(mock_popen):
    """不认识的占位符 {missing} 原样保留，不报错"""
    cmd = VoiceCommand(
        id="e5", name="", matchType="prefix", matchPattern="",
        actionType="exec", actionParams="unknown {missing}", enabled=True
    )
    execute_command(cmd, all_text="test", prefix="", content="")
    mock_popen.assert_called_once()
    args, _ = mock_popen.call_args
    # {missing} 原样保留在 token 中
    assert "{missing}" in args[0]


def test_execute_command_unknown_action_type(caplog):
    cmd = VoiceCommand(
        id="e6", name="", matchType="prefix", matchPattern="",
        actionType="invalid", actionParams="", enabled=True
    )
    with caplog.at_level(logging.ERROR):
        execute_command(cmd, all_text="", prefix="", content="")
    assert "未知动作类型" in caplog.text


# ---------- exec 测试：Windows 路径反斜杠 ----------
@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_exec_windows_backslash_path(mock_popen):
    """Windows 路径反斜杠不被破坏（pyparsing 替代 shlex 的核心优势）"""
    cmd = VoiceCommand(
        id="e7", name="", matchType="prefix", matchPattern="",
        actionType="exec",
        actionParams=r'python "C:\Users\name\script.py"',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    args, _ = mock_popen.call_args
    assert args[0] == ['python', r'C:\Users\name\script.py']


# ---------- 新增测试：{cwd} 自定义工作目录 ----------
@patch("phonemic.utils.command_processor.subprocess.Popen")
@patch("phonemic.utils.command_processor.get_exec_workdir")
def test_execute_command_with_cwd_custom_cwd(mock_get_workdir, mock_popen):
    """验证 {cwd:"D:\\temp"} 会设置 cwd 且 token 被移除"""
    mock_get_workdir.return_value = "C:\\fake\\default"

    cmd = VoiceCommand(
        id="cwd1", name="", matchType="prefix", matchPattern="",
        actionType="exec",
        actionParams='{cwd:"D:\\temp"} python script.py arg',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    # 验证 token 被移除，实际命令参数为 ['python', 'script.py', 'arg']
    assert args[0] == ['python', 'script.py', 'arg']
    # 验证 cwd 被设置为自定义路径
    assert kwargs.get("cwd") == "D:\\temp"
    # 验证默认工作目录没有被调用（因为自定义优先）
    mock_get_workdir.assert_not_called()


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_with_cwd_quoted_with_spaces(mock_popen):
    """验证带空格的路径（用引号包围）被正确解析"""
    cmd = VoiceCommand(
        id="cwd2", name="", matchType="prefix", matchPattern="",
        actionType="exec",
        actionParams='{cwd:"C:\\Program Files\\MyApp"} python script.py',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    args, kwargs = mock_popen.call_args
    assert args[0] == ['python', 'script.py']
    assert kwargs.get("cwd") == "C:\\Program Files\\MyApp"


@patch("phonemic.utils.command_processor.subprocess.Popen")
def test_execute_command_with_cwd_no_quotes(mock_popen):
    """验证无引号且无空格的路径"""
    cmd = VoiceCommand(
        id="cwd3", name="", matchType="prefix", matchPattern="",
        actionType="exec",
        actionParams='{cwd:D:\\data} python script.py',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    args, kwargs = mock_popen.call_args
    assert args[0] == ['python', 'script.py']
    assert kwargs.get("cwd") == "D:\\data"


@patch("phonemic.utils.command_processor.subprocess.Popen")
@patch("phonemic.utils.command_processor.get_exec_workdir")
def test_execute_command_without_cwd_uses_default_cwd(mock_get_workdir, mock_popen):
    """没有 {cwd} 时，应该使用默认工作目录"""
    mock_get_workdir.return_value = "C:\\fake\\default"
    cmd = VoiceCommand(
        id="cwd4", name="", matchType="prefix", matchPattern="",
        actionType="exec",
        actionParams='python script.py',
        enabled=True
    )
    execute_command(cmd, all_text="", prefix="", content="")
    args, kwargs = mock_popen.call_args
    assert args[0] == ['python', 'script.py']
    assert kwargs.get("cwd") == "C:\\fake\\default"
    mock_get_workdir.assert_called_once()

# 此函数废弃，由于windows使用shlex.split的问题，cwd只能放在最前面
#@patch("phonemic.utils.command_processor.subprocess.Popen")
#def test_execute_command_cwd_placement_anywhere(mock_popen):
#    """{cwd} 可以出现在参数任意位置，且只移除第一个"""
#    cmd = VoiceCommand(
#        id="cwd5", name="", matchType="prefix", matchPattern="",
#        actionType="exec",
#        actionParams='python {cwd:"C:\\work"} script.py',
#        enabled=True
#    )
#    execute_command(cmd, all_text="", prefix="", content="")
#    args, kwargs = mock_popen.call_args
#    # 移除 {cwd} 后，参数应为 ['python', 'script.py']
#    assert args[0] == ['python', 'script.py']
#    assert kwargs.get("cwd") == "C:\\work"
