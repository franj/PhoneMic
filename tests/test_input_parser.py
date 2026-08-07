"""
单元测试：input_parser 模块
覆盖 parse_input_sequence, parse_exec_args, apply_template 三个函数。
"""
import pytest
import pyparsing as pp
from datetime import datetime
from unittest.mock import patch

from phonemic.utils.input_parser import (
    parse_input_sequence,
    parse_exec_args,
    apply_template,
)


# ==================== parse_input_sequence ====================

class TestParseInputSequence:

    def test_pure_keys(self):
        """纯按键序列（无引号），向后兼容"""
        result = parse_input_sequence("ctrl+a, delete")
        assert result == [("key", "ctrl+a"), ("key", "delete")]

    def test_single_key(self):
        result = parse_input_sequence("enter")
        assert result == [("key", "enter")]

    def test_pure_text_double_quotes(self):
        """纯粘贴文本（双引号）"""
        result = parse_input_sequence('"hello world"')
        assert result == [("text", "hello world")]

    def test_pure_text_single_quotes(self):
        """纯粘贴文本（单引号）"""
        result = parse_input_sequence("'hello world'")
        assert result == [("text", "hello world")]

    def test_mixed_keys_and_text(self):
        """混合：按键 + 文本 + 按键"""
        result = parse_input_sequence('ctrl+a, "hello", left')
        assert result == [
            ("key", "ctrl+a"),
            ("text", "hello"),
            ("key", "left"),
        ]

    def test_escaped_double_quote_inside_dq(self):
        """双引号内用 \\\" 转义字面双引号"""
        result = parse_input_sequence(r'"\"\""')
        assert result == [("text", '""')]

    def test_escaped_single_quote_inside_sq(self):
        """单引号内用 \\' 转义字面单引号"""
        result = parse_input_sequence(r"'it\'s'")
        assert result == [("text", "it's")]

    def test_comma_inside_quotes(self):
        """引号内的逗号不算分隔符"""
        result = parse_input_sequence('"a,b,c", enter')
        assert result == [("text", "a,b,c"), ("key", "enter")]

    def test_template_placeholders_in_text(self):
        """引号内的占位符原样返回（替换在 apply_template 做）"""
        result = parse_input_sequence('"time: {time}", enter')
        assert result == [("text", "time: {time}"), ("key", "enter")]

    def test_double_quotes_with_backslash_n(self):
        """反斜杠在双引号段里只做转义符（escChar='\\'），
        非转义序列的 \\ 原样保留"""
        # \\\" → \"（一个字面的双引号）
        result = parse_input_sequence(r'"path\"end"')
        assert result == [("text", 'path"end')]

    def test_empty_string(self):
        assert parse_input_sequence("") == []

    def test_whitespace_only(self):
        assert parse_input_sequence("   ") == []

    def test_single_quotes_with_double_quote_inside(self):
        """单引号段里可以自由包含双引号"""
        result = parse_input_sequence("""'say "hello"'""")
        assert result == [("text", 'say "hello"')]

    def test_double_quotes_with_single_quote_inside(self):
        """双引号段里可以自由包含单引号"""
        result = parse_input_sequence('"it\'s ok"')
        assert result == [("text", "it's ok")]

    def test_spaces_around_key_segments(self):
        """按键段前后空格被 strip"""
        result = parse_input_sequence(' ctrl+a , delete ')
        assert result == [("key", "ctrl+a"), ("key", "delete")]

    def test_empty_key_segment_skipped(self):
        """逗号之间的空段（无内容）应抛出 ParseException"""
        with pytest.raises(pp.ParseException):
            parse_input_sequence('"hello",  , enter')

    def test_unclosed_quote_raises(self):
        """未闭合的引号应抛异常"""
        with pytest.raises(pp.ParseException):
            parse_input_sequence('"unclosed')
        with pytest.raises(pp.ParseException):
            parse_input_sequence("'also unclosed, enter")

    def test_max_realistic_scenario(self):
        """接近真实使用场景的复杂序列"""
        seq = r'ctrl+a, "替换内容", ctrl+v, "\"\"", left, enter'
        result = parse_input_sequence(seq)
        assert result == [
            ("key", "ctrl+a"),
            ("text", "替换内容"),
            ("key", "ctrl+v"),
            ("text", '""'),
            ("key", "left"),
            ("key", "enter"),
        ]

    def test_chinese_text(self):
        result = parse_input_sequence('"你好世界", enter')
        assert result == [("text", "你好世界"), ("key", "enter")]


# ==================== parse_exec_args ====================

class TestParseExecArgs:

    def test_simple_command(self):
        assert parse_exec_args("calc") == ["calc"]

    def test_command_with_args(self):
        assert parse_exec_args("python script.py") == ["python", "script.py"]

    def test_double_quoted_path_with_backslashes(self):
        """Windows 路径反斜杠不被破坏"""
        result = parse_exec_args(r'python "C:\Users\name\script.py"')
        assert result == ["python", r"C:\Users\name\script.py"]

    def test_single_quoted_path(self):
        result = parse_exec_args("python '/home/user/script.py'")
        assert result == ["python", "/home/user/script.py"]

    def test_path_with_spaces(self):
        """带空格的路径用引号包围"""
        result = parse_exec_args(r'python "C:\Program Files\app\run.py"')
        assert result == ["python", r"C:\Program Files\app\run.py"]

    def test_multiple_args(self):
        result = parse_exec_args("echo hello world")
        assert result == ["echo", "hello", "world"]

    def test_empty_string(self):
        assert parse_exec_args("") == []

    def test_whitespace_only(self):
        assert parse_exec_args("   ") == []

    def test_double_quote_escape_by_doubling(self):
        """双引号内用 "" 转义字面双引号"""
        result = parse_exec_args('python -c "print(""hello"")"')
        assert result == ["python", "-c", 'print("hello")']

    def test_single_quote_escape_by_doubling(self):
        """单引号内用 '' 转义字面单引号"""
        result = parse_exec_args("python -c 'print(''hello'')'")
        assert result == ["python", "-c", "print('hello')"]

    def test_mixed_quotes(self):
        result = parse_exec_args("""python "arg with 'quotes'" """)
        assert result == ["python", "arg with 'quotes'"]

    def test_chinese_path(self):
        result = parse_exec_args(r'python "D:\我的文档\script.py" arg1')
        assert result == ["python", r"D:\我的文档\script.py", "arg1"]

    def test_backslash_not_escape(self):
        """反斜杠后跟非特殊字符时原样保留"""
        result = parse_exec_args(r"python C:\path\no\slash\issues")
        assert result == ["python", r"C:\path\no\slash\issues"]


# ==================== apply_template ====================

class TestApplyTemplate:

    def test_time_placeholder(self):
        with patch('phonemic.utils.input_parser.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 7, 17, 30, 25)
            result = apply_template("now: {time}")
            assert result == "now: 17:30:25"

    def test_date_placeholder(self):
        with patch('phonemic.utils.input_parser.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 7, 12, 0, 0)
            result = apply_template("today: {date}")
            assert result == "today: 2026-08-07"

    def test_content_placeholder(self):
        result = apply_template("say: {content}", content="hello")
        assert result == "say: hello"

    def test_prefix_placeholder(self):
        result = apply_template("[{prefix}]", prefix="记录 ")
        assert result == "[记录 ]"

    def test_all_text_placeholder(self):
        result = apply_template("log: {all_text}", all_text="记录 开心")
        assert result == "log: 记录 开心"

    def test_multiple_placeholders(self):
        with patch('phonemic.utils.input_parser.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 7, 9, 15, 30)
            result = apply_template(
                "[{date} {time}] {content}", content="hello"
            )
            assert result == "[2026-08-07 09:15:30] hello"

    def test_unknown_placeholder_preserved(self):
        """不认识的 {xxx} 原样保留"""
        result = apply_template("{unknown} stays")
        assert result == "{unknown} stays"

    def test_no_placeholders(self):
        result = apply_template("just text")
        assert result == "just text"

    def test_empty_content(self):
        result = apply_template("say: {content}", content="")
        assert result == "say: "

    def test_multiple_same_placeholder(self):
        """同一个占位符出现多次，全部替换"""
        result = apply_template("{content} and {content}", content="x")
        assert result == "x and x"

    def test_literal_braces_without_placeholder(self):
        """字面 { } 不匹配任何已知占位符时保留"""
        result = apply_template("{not_a_var} and {time}")
        # {not_a_var} 原样保留, {time} 被替换
        assert "{not_a_var}" in result
        assert "{time}" not in result

    def test_chinese_text_with_placeholders(self):
        result = apply_template("时间：{time}，内容：{content}", content="测试")
        assert "内容：测试" in result
        assert "时间：" in result
        assert "{time}" not in result
