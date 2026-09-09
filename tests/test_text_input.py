"""
单元测试：模拟键盘直接输入模块 (phonemic.gui.text_input)

用桩替换 Win32 层（INPUT 结构体 + SendInput），因此在任意平台都能运行，
验证的是「字符 -> 键盘事件」的编码规则：
中文、全角标点、emoji 代理项对、换行、制表符、分批边界、失败处理。
"""
import pytest

from phonemic.gui import text_input


# ---------- Win32 层的桩 ----------
class _FakeKeybdInput:
    def __init__(self):
        self.wVk = 0
        self.wScan = 0
        self.dwFlags = 0
        self.time = 0
        self.dwExtraInfo = 0


class _FakeUnion:
    def __init__(self):
        self.ki = _FakeKeybdInput()


class FakeInput:
    """与 Win32 INPUT 字段访问方式一致的桩"""

    def __init__(self):
        self.type = 0
        self.union = _FakeUnion()


@pytest.fixture
def injected(monkeypatch):
    """把 text_input 切到桩实现，返回按批次记录的注入事件列表"""
    batches = []

    monkeypatch.setattr(text_input, "IS_WINDOWS", True)
    monkeypatch.setattr(text_input, "INPUT", FakeInput)
    monkeypatch.setattr(text_input, "BATCH_INTERVAL_SEC", 0)
    monkeypatch.setattr(text_input, "_send", lambda events: batches.append(list(events)))
    return batches


def flatten(batches):
    return [e for b in batches for e in b]


def decode(events):
    """把注入的事件序列还原成文本，用于验证编码正确性"""
    out, units = [], []

    def flush():
        if units:
            out.append(b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le"))
            units.clear()

    for e in events:
        if e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP:
            continue
        if e.union.ki.dwFlags & text_input.KEYEVENTF_UNICODE:
            units.append(e.union.ki.wScan)
        else:
            flush()
            mapped = {text_input.VK_RETURN: "\n", text_input.VK_TAB: "\t"}.get(e.union.ki.wVk)
            if mapped:
                out.append(mapped)
    flush()
    return "".join(out)


# ---------- 编码正确性 ----------
@pytest.mark.parametrize("text", [
    "hello world",
    "ls -la /usr/bin | grep python",
    "你好，世界！",
    "全角标点：（）《》、；“”",
    "emoji 😀🚀 混排",
    "混合 ABC 中文 123 😀 结束",
    "制表\t符",
    "多行\n第二行",
])
def test_send_text_roundtrip(injected, text):
    """注入的事件序列解码后应与原文一致"""
    text_input.send_text(text)
    assert decode(flatten(injected)) == text


def test_crlf_normalized_to_single_return(injected):
    """\\r\\n 只产生一次回车，不会敲两下"""
    text_input.send_text("a\r\nb")
    events = flatten(injected)
    returns = [e for e in events
               if not e.union.ki.dwFlags & text_input.KEYEVENTF_UNICODE
               and e.union.ki.wVk == text_input.VK_RETURN
               and not e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP]
    assert len(returns) == 1
    assert decode(events) == "a\nb"


def test_newline_uses_shift_return_not_unicode(injected):
    """换行走 Shift+Enter，避免聊天软件把单独的 Enter 当成发送"""
    text_input.send_text("\n")
    events = flatten(injected)
    assert len(events) == 4
    assert [e.union.ki.wVk for e in events] == [
        text_input.VK_SHIFT, text_input.VK_RETURN,
        text_input.VK_RETURN, text_input.VK_SHIFT,
    ]
    assert [bool(e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP) for e in events] == [
        False, False, True, True,
    ]
    assert all(not e.union.ki.dwFlags & text_input.KEYEVENTF_UNICODE for e in events)


def test_newline_shift_does_not_wrap_neighboring_chars(injected):
    """Shift 只包住 Enter 本身，前后字符不应被带着按 Shift"""
    text_input.send_text("a\nb")
    events = flatten(injected)
    vks = [e.union.ki.wVk for e in events]
    assert vks[:2] == [0, 0]  # 'a' 的 unicode 按下/抬起，wVk 为 0
    assert vks[2:6] == [
        text_input.VK_SHIFT, text_input.VK_RETURN,
        text_input.VK_RETURN, text_input.VK_SHIFT,
    ]
    assert vks[6:] == [0, 0]  # 'b'


def test_consecutive_newlines_each_get_shift_return(injected):
    text_input.send_text("\n\n")
    events = flatten(injected)
    assert len(events) == 8
    returns = [e for e in events
               if e.union.ki.wVk == text_input.VK_RETURN
               and not e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP]
    assert len(returns) == 2


def test_each_char_has_keydown_and_keyup(injected):
    """每个码元都要成对的按下/抬起事件"""
    text_input.send_text("ab")
    events = flatten(injected)
    assert len(events) == 4
    assert [bool(e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP) for e in events] == \
        [False, True, False, True]


def test_emoji_uses_surrogate_pair(injected):
    """BMP 外字符拆成两个 UTF-16 码元，共 4 个事件"""
    text_input.send_text("😀")
    events = flatten(injected)
    assert len(events) == 4
    units = [e.union.ki.wScan for e in events
             if not e.union.ki.dwFlags & text_input.KEYEVENTF_KEYUP]
    assert units == [0xD83D, 0xDE00]


def test_control_chars_dropped(injected):
    """除换行与制表符外的控制字符被丢弃"""
    text_input.send_text("a\x00\x07b")
    assert decode(flatten(injected)) == "ab"


def test_only_control_chars_sends_nothing(injected):
    text_input.send_text("\x00\x07")
    assert injected == []


def test_empty_text_sends_nothing(injected):
    text_input.send_text("")
    assert injected == []


def test_non_string_raises(injected):
    with pytest.raises(TypeError):
        text_input.send_text(123)


# ---------- 分批 ----------
def test_batches_respect_max_and_keep_surrogate_pairs_intact(injected, monkeypatch):
    """分批不得把一个字符（含代理项对）拆到两个 SendInput 调用里"""
    monkeypatch.setattr(text_input, "MAX_INPUTS_PER_CALL", 5)
    text_input.send_text("a😀b😀c")

    assert len(injected) > 1, "应当分成多批"
    for batch in injected:
        units = [e.union.ki.wScan for e in batch
                 if e.union.ki.dwFlags == text_input.KEYEVENTF_UNICODE]
        # 批次内若出现孤立代理项，这里会抛 UnicodeDecodeError
        b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le")
    assert decode(flatten(injected)) == "a😀b😀c"


def test_long_text_split_into_multiple_batches(injected):
    text = "x" * 500
    text_input.send_text(text)
    assert len(injected) > 1
    assert all(len(b) <= text_input.MAX_INPUTS_PER_CALL for b in injected)
    assert decode(flatten(injected)) == text


# ---------- 失败与平台限制 ----------
def test_first_batch_failure_reports_not_partial(monkeypatch):
    """首批就失败 -> partial=False，上层可安全回退到剪贴板"""
    monkeypatch.setattr(text_input, "IS_WINDOWS", True)
    monkeypatch.setattr(text_input, "INPUT", FakeInput)
    monkeypatch.setattr(text_input, "BATCH_INTERVAL_SEC", 0)
    monkeypatch.setattr(
        text_input, "_send",
        lambda events: (_ for _ in ()).throw(text_input.SendTextError("拦截", partial=False)))

    with pytest.raises(text_input.SendTextError) as exc:
        text_input.send_text("hello")
    assert exc.value.partial is False


def test_later_batch_failure_reports_partial(monkeypatch):
    """前面批次已注入、后续批次失败 -> partial=True，上层不得回退，否则重复上屏"""
    monkeypatch.setattr(text_input, "IS_WINDOWS", True)
    monkeypatch.setattr(text_input, "INPUT", FakeInput)
    monkeypatch.setattr(text_input, "BATCH_INTERVAL_SEC", 0)
    monkeypatch.setattr(text_input, "MAX_INPUTS_PER_CALL", 4)

    calls = []

    def flaky(events):
        calls.append(events)
        if len(calls) > 1:
            raise text_input.SendTextError("中断", partial=False)

    monkeypatch.setattr(text_input, "_send", flaky)
    with pytest.raises(text_input.SendTextError) as exc:
        text_input.send_text("abcdef")
    assert exc.value.partial is True


def test_partial_within_batch_reports_partial(monkeypatch):
    """同一批内只注入了一部分 -> partial=True"""
    monkeypatch.setattr(text_input, "IS_WINDOWS", True)
    monkeypatch.setattr(text_input, "INPUT", FakeInput)
    monkeypatch.setattr(text_input, "BATCH_INTERVAL_SEC", 0)
    monkeypatch.setattr(
        text_input, "_send",
        lambda events: (_ for _ in ()).throw(text_input.SendTextError("半截", partial=True)))

    with pytest.raises(text_input.SendTextError) as exc:
        text_input.send_text("hello")
    assert exc.value.partial is True


def test_non_windows_raises(monkeypatch):
    monkeypatch.setattr(text_input, "IS_WINDOWS", False)
    with pytest.raises(text_input.SendTextError, match="仅在 Windows"):
        text_input.send_text("hello")
