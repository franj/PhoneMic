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
            out.append({text_input.VK_RETURN: "\n", text_input.VK_TAB: "\t"}[e.union.ki.wVk])
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


def test_newline_uses_virtual_key_not_unicode(injected):
    """换行必须走 VK_RETURN，Unicode 注入 \\n 在多数程序里不产生回车"""
    text_input.send_text("\n")
    events = flatten(injected)
    assert len(events) == 2
    assert all(e.union.ki.wVk == text_input.VK_RETURN for e in events)
    assert all(not e.union.ki.dwFlags & text_input.KEYEVENTF_UNICODE for e in events)


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
def test_send_failure_propagates(monkeypatch):
    """SendInput 未全部注入时抛 RuntimeError，供上层回退到剪贴板"""
    monkeypatch.setattr(text_input, "IS_WINDOWS", True)
    monkeypatch.setattr(text_input, "INPUT", FakeInput)
    monkeypatch.setattr(text_input, "BATCH_INTERVAL_SEC", 0)

    def boom(events):
        raise RuntimeError("SendInput 注入失败")

    monkeypatch.setattr(text_input, "_send", boom)
    with pytest.raises(RuntimeError):
        text_input.send_text("hello")


def test_non_windows_raises(monkeypatch):
    monkeypatch.setattr(text_input, "IS_WINDOWS", False)
    with pytest.raises(RuntimeError, match="仅在 Windows"):
        text_input.send_text("hello")


# ---------- 终端识别 ----------
@pytest.mark.parametrize("class_name,process_name,expected", [
    ("cascadia_hosting_window_class", "windowsterminal.exe", True),
    ("consolewindowclass", "cmd.exe", True),
    ("mintty", "mintty.exe", True),
    ("putty", "putty.exe", True),
    ("", "pwsh.exe", True),
    ("notepad", "notepad.exe", False),
    ("chrome_widgetwin_1", "chrome.exe", False),
    ("", "", False),
])
def test_is_terminal_foreground(monkeypatch, class_name, process_name, expected):
    monkeypatch.setattr(text_input, "get_foreground_app", lambda: (class_name, process_name))
    assert text_input.is_terminal_foreground() is expected


def test_is_terminal_foreground_extra_processes(monkeypatch):
    """用户自定义的冷门终端也应被识别"""
    monkeypatch.setattr(text_input, "get_foreground_app", lambda: ("someclass", "myterm.exe"))
    assert text_input.is_terminal_foreground() is False
    assert text_input.is_terminal_foreground(["MyTerm.exe"]) is True
    assert text_input.is_terminal_foreground(["  ", ""]) is False


def test_get_foreground_app_non_windows(monkeypatch):
    monkeypatch.setattr(text_input, "IS_WINDOWS", False)
    assert text_input.get_foreground_app() == ("", "")
