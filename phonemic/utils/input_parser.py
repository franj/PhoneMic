"""
混合输入序列解析与模板替换。

- parse_input_sequence: 解析 key 类型的 actionParams，支持按键组合和粘贴文本混合。
- parse_exec_args: 替代 shlex.split，Windows 反斜杠友好。
- apply_template: 统一的占位符替换，exec 和粘贴文本共用。
"""
import logging
from datetime import datetime
from typing import List, Tuple

import pyparsing as pp

logger = logging.getLogger(__name__)

# ---------- 输入序列解析 (key 类型) ----------

def parse_input_sequence(s: str) -> List[Tuple[str, str]]:
    """
    解析混合输入序列，返回 [(type, value), ...]。

    - 引号段 → ('text', 引号内文本)
    - 非引号段 → ('key', 按键组合字符串)

    支持的引号类型：
    - ASCII 双引号 "..."  ：支持 \\" 转义
    - ASCII 单引号 '...'  ：支持 \\' 转义
    - 全角双引号 “...”   ：U+201C/U+201D 配对（无需转义，左右字符不同）
    - 全角单引号 ‘...’   ：U+2018/U+2019 配对

    段之间用逗号分隔，引号内的逗号不算分隔符。

    示例:
        'ctrl+a, "hello", left'  → [('key','ctrl+a'), ('text','hello'), ('key','left')]
        '"\\"\\"", left'         → [('text','""'), ('key','left')]
        '‘“”’, left'             → [('text','“”'), ('key','left')]   # 全角引号
        '"time: {time}", enter'  → [('text','time: {time}'), ('key','enter')]
    """
    if not s or not s.strip():
        return []

    segments: List[Tuple[str, str]] = []

    # ASCII 双引号段：支持 \" 转义
    dq = pp.QuotedString('"', esc_char='\\')
    # ASCII 单引号段：支持 \' 转义
    sq = pp.QuotedString("'", esc_char='\\')

    # 全角双引号段：U+201C ... U+201D（左右字符不同，无需转义）
    # set_parse_action 返回剥去首尾引号后的内容
    cn_dq = pp.Regex(r'\u201c[^\u201d]*\u201d').set_parse_action(
        lambda t: [t[0][1:-1]]
    )

    # 全角单引号段：U+2018 ... U+2019
    cn_sq = pp.Regex(r'\u2018[^\u2019]*\u2019').set_parse_action(
        lambda t: [t[0][1:-1]]
    )

    # 引号段 → text（QuotedString 已自动剥引号，Regex 的 parse_action 已剥引号）
    quoted = (dq | sq | cn_dq | cn_sq).add_parse_action(
        lambda t: segments.append(('text', t[0]))
    )

    # 非引号非逗号段 → key（排除所有引号字符）
    unquoted = pp.Regex(r'[^"\',\u201c\u201d\u2018\u2019]+').add_parse_action(
        lambda t: segments.append(('key', t[0].strip())) if t[0].strip() else None
    )

    grammar = pp.DelimitedList(quoted | unquoted, delim=',')
    grammar.parse_string(s, parse_all=True)
    return segments


# ---------- exec 参数解析 (替代 shlex) ----------

def parse_exec_args(s: str) -> List[str]:
    """
    替代 shlex.split，Windows 反斜杠友好。

    规则:
    - 双引号包围的内容作为一个 token，用 "" 转义字面的双引号
    - 单引号包围的内容作为一个 token，用 '' 转义字面的单引号
    - 反斜杠是普通字符，不做转义
    - 空白分隔 token

    示例:
        'python "C:\\Users\\name\\script.py"'
        → ['python', 'C:\\Users\\name\\script.py']
    """
    if not s or not s.strip():
        return []

    tokens: List[str] = []

    # 双引号段：用 Regex 精确控制，"" → "，反斜杠不特殊
    dq = pp.Regex(r'"(?:[^"]|"")*"')
    dq.set_parse_action(lambda t: tokens.append(t[0][1:-1].replace('""', '"')))

    # 单引号段：'' → '
    sq = pp.Regex(r"'(?:[^']|'')*'")
    sq.set_parse_action(lambda t: tokens.append(t[0][1:-1].replace("''", "'")))

    # 非引号非空白
    unq = pp.Regex(r'[^\s"\']+')
    unq.set_parse_action(lambda t: tokens.append(t[0]))

    grammar = pp.OneOrMore(dq | sq | unq)
    grammar.parse_string(s, parse_all=True)
    return tokens


# ---------- 模板替换 (exec 和粘贴文本共用) ----------

def apply_template(text: str, all_text: str = "", prefix: str = "", content: str = "") -> str:
    """
    统一占位符替换。全部用 str.replace，不用 str.format。

    支持的占位符:
    - {time}     → 当前时间 HH:MM:SS
    - {date}     → 今天日期 YYYY-MM-DD
    - {content}  → 传入的 content
    - {prefix}   → 传入的 prefix
    - {all_text} → 传入的 all_text

    不认识的 {xxx} 原样保留。
    """
    now = datetime.now()
    text = text.replace('{time}', now.strftime('%H:%M:%S'))
    text = text.replace('{date}', now.strftime('%Y-%m-%d'))
    text = text.replace('{content}', content)
    text = text.replace('{prefix}', prefix)
    text = text.replace('{all_text}', all_text)
    return text
