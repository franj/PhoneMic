"""
单元测试：直接输入模式 (phonemic.gui.direct_input)

验证会话状态机与增量 diff：
- 只敲「新增」的部分、只删「差异」的部分；
- 焦点变化时放弃会话，且已经打上屏的文字不会被补发造成重复；
- 各种「有没有会话 / 有没有字上屏」组合下，给调用方的返回值是否正确。

真正的键盘注入（text_input.send_text / send_backspace）被打桩，
焦点探测由测试注入，因此不依赖真实窗口、也不会真的敲键盘。
"""
import pytest
from unittest.mock import patch

from phonemic.gui import direct_input
from phonemic.gui.direct_input import DirectInputController, IDLE, TYPING, ABANDONED
from phonemic.gui.text_input import SendTextError


class _Focus:
    """可在测试中改写的焦点指纹探针"""

    def __init__(self, sig=(1, 1)):
        self.sig = sig

    def __call__(self):
        return self.sig


def make_controller(sig=(1, 1)):
    """返回 (控制器, 焦点探针)"""
    focus = _Focus(sig)
    return DirectInputController(focus), focus


@pytest.fixture
def rec():
    """拦截键盘注入，返回 (打出的文字列表, 每次退格的次数列表)"""
    typed, deleted = [], []
    with patch.object(direct_input.text_input, "send_text",
                      side_effect=lambda t: typed.append(t)), \
         patch.object(direct_input.text_input, "send_backspace",
                      side_effect=lambda n: deleted.append(n)):
        yield typed, deleted


# ---------- 公共前缀 diff ----------
@pytest.mark.parametrize("a,b,expected", [
    ("", "", 0),
    ("你好", "你好世界", 2),
    ("你好世界", "你好石阶", 2),
    ("abc", "abc", 3),
    ("abc", "xyz", 0),
    ("😀a", "😀b", 1),   # emoji 算一个码点
])
def test_common_prefix_len(a, b, expected):
    assert direct_input._common_prefix_len(a, b) == expected


def test_real_focus_signature_readable():
    """真实环境下焦点指纹可读：一对非负整数句柄"""
    if not direct_input.IS_WINDOWS:  # pragma: no cover
        pytest.skip("焦点探测依赖 Win32")
    sig = direct_input._focus_signature()
    assert isinstance(sig, tuple) and len(sig) == 2
    assert all(isinstance(v, int) and v >= 0 for v in sig)


# ---------- 增量更新 ----------
class TestUpdate:
    def test_first_preview_starts_session(self, rec):
        typed, deleted = rec
        ctrl, _ = make_controller()
        assert ctrl.update("你好") is True
        assert typed == ["你好"]
        assert deleted == []
        assert ctrl.typed == "你好"
        assert ctrl.state == TYPING

    def test_appending_only_types_the_new_part(self, rec):
        """语音识别最常见的情况：往后追加，不该重打已有内容"""
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好")
        ctrl.update("你好世界")
        assert typed == ["你好", "世界"]
        assert deleted == []

    def test_tail_correction_deletes_then_types(self, rec):
        """末尾修正：退格删掉差异部分，再敲新的"""
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好世界")
        ctrl.update("你好石阶")
        assert deleted == [2]
        assert typed == ["你好世界", "石阶"]
        assert ctrl.typed == "你好石阶"

    def test_identical_text_is_noop(self, rec):
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好")
        ctrl.update("你好")
        assert typed == ["你好"]
        assert deleted == []

    def test_empty_preview_before_session_is_not_handled(self, rec):
        """还没有任何内容时，空 preview 不值得开启会话"""
        ctrl, _ = make_controller()
        assert ctrl.update("") is False
        assert ctrl.state == IDLE
        assert rec[0] == [] and rec[1] == []

    def test_empty_preview_clears_what_was_typed(self, rec):
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好")
        assert ctrl.update("") is True
        assert deleted == [2]
        assert typed == ["你好"]
        assert ctrl.typed == ""

    def test_emoji_backspace_counts_one(self, rec):
        """退格按码点计，一个 emoji 一次退格"""
        _, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("😀")
        ctrl.update("好")
        assert deleted == [1]

    def test_non_str_raises(self):
        ctrl, _ = make_controller()
        with pytest.raises(TypeError):
            ctrl.update(123)


# ---------- 焦点保护 ----------
class TestFocusGuard:
    def test_focus_change_stops_typing(self, rec):
        typed, _ = rec
        ctrl, focus = make_controller()
        ctrl.update("你好")
        focus.sig = (2, 2)                     # 用户切了窗口
        assert ctrl.update("你好世界") is False
        assert typed == ["你好"]                # 不再往新窗口里打
        assert ctrl.state == ABANDONED

    def test_updates_after_abandon_are_all_rejected(self, rec):
        typed, _ = rec
        ctrl, focus = make_controller()
        ctrl.update("你好")
        focus.sig = (2, 2)
        ctrl.update("你好世界")
        assert ctrl.update("你好世界啊") is False
        assert typed == ["你好"]

    def test_focus_change_at_commit_suppresses_duplicate(self, rec):
        """已上屏过就不许调用方再补一次，否则同一段话出现两遍"""
        typed, _ = rec
        ctrl, focus = make_controller()
        ctrl.update("你好")
        focus.sig = (2, 2)
        assert ctrl.commit("你好世界") is True
        assert typed == ["你好"]
        assert ctrl.state == IDLE

    def test_focus_change_before_any_text_allows_fallback(self, rec):
        """一个字都没上屏时，调用方可以放心改用其它上屏方式"""
        ctrl, focus = make_controller()
        ctrl._begin()          # 会话已开启
        focus.sig = (2, 2)
        assert ctrl.update("你好") is False
        assert ctrl.dirty is False
        assert ctrl.commit("你好") is False

    def test_anchor_is_captured_at_session_start(self, rec):
        ctrl, focus = make_controller((7, 7))
        ctrl.update("你")
        focus.sig = (7, 8)     # 同一窗口、换了控件也要停
        assert ctrl.update("你好") is False


# ---------- 提交 ----------
class TestCommit:
    def test_commit_without_session_returns_false(self, rec):
        """客户端只发 send（没有 preview）时，交给调用方按普通方式上屏"""
        ctrl, _ = make_controller()
        assert ctrl.commit("你好") is False
        assert rec[0] == []

    def test_commit_applies_final_diff_and_resets(self, rec):
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好")
        assert ctrl.commit("你好世界") is True
        assert typed == ["你好", "世界"]
        assert deleted == []
        assert ctrl.state == IDLE
        assert ctrl.typed == ""

    def test_commit_equal_to_typed_does_nothing(self, rec):
        """识别结果没有再变化时，提交是空操作——这也是直接输入最理想的情况"""
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好")
        assert ctrl.commit("你好") is True
        assert typed == ["你好"]
        assert deleted == []

    def test_commit_can_shrink_text(self, rec):
        typed, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("你好世界")
        assert ctrl.commit("你好") is True
        assert deleted == [2]
        assert typed == ["你好世界"]

    def test_non_str_raises(self):
        ctrl, _ = make_controller()
        with pytest.raises(TypeError):
            ctrl.commit(None)

    def test_reset_clears_state(self, rec):
        ctrl, _ = make_controller()
        ctrl.update("你好")
        ctrl.reset()
        assert ctrl.state == IDLE
        assert ctrl.typed == ""
        assert ctrl.dirty is False


# ---------- 注入失败 ----------
class TestInjectionFailure:
    def test_first_failure_without_text_allows_fallback(self, rec):
        """一个字都没进去 -> 放弃会话并让调用方回退"""
        typed, deleted = rec
        ctrl, _ = make_controller()
        with patch.object(direct_input.text_input, "send_text",
                          side_effect=SendTextError("UIPI 拦截", partial=False)):
            assert ctrl.update("你好") is False
        assert ctrl.state == ABANDONED
        assert ctrl.dirty is False
        assert ctrl.commit("你好") is False

    def test_partial_failure_after_text_is_not_rewound(self, rec):
        """已经打了一部分 -> 不得回退，否则重复上屏"""
        typed, _ = rec
        ctrl, _ = make_controller()
        ctrl.update("你")
        with patch.object(direct_input.text_input, "send_text",
                          side_effect=SendTextError("中途中断", partial=True)):
            with pytest.raises(SendTextError):
                ctrl.update("你好")
        assert ctrl.state == ABANDONED
        assert ctrl.commit("你好") is True   # 不许调用方补发


# ---------- 撤销（命中语音命令时） ----------
class TestDiscard:
    def test_discard_without_session_is_noop(self, rec):
        """没有会话（客户端只发了 send）时无需撤销"""
        typed, deleted = rec
        ctrl, _ = make_controller()
        assert ctrl.discard() is True
        assert typed == [] and deleted == []
        assert ctrl.state == IDLE

    def test_discard_deletes_what_was_typed(self, rec):
        """命令执行前把 preview 打出的字面文字删干净"""
        _, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("回车")
        assert ctrl.discard() is True
        assert deleted == [2]
        assert ctrl.state == IDLE
        assert ctrl.typed == ""

    def test_discard_counts_code_points(self, rec):
        """退格按码点计：emoji 一次退格，与注入粒度一致"""
        _, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("😀好")
        assert ctrl.discard() is True
        assert deleted == [2]

    def test_discard_without_any_text_is_safe(self, rec):
        """会话开了但一个字都没打（首帧就失败了）时无需退格"""
        _, deleted = rec
        ctrl, _ = make_controller()
        ctrl._begin()
        assert ctrl.discard() is True
        assert deleted == []

    def test_discard_after_focus_change_sends_no_backspace(self, rec):
        """焦点变了绝不能发退格——那会删掉新窗口里用户自己的内容"""
        _, deleted = rec
        ctrl, focus = make_controller()
        ctrl.update("回车")
        focus.sig = (2, 2)
        assert ctrl.discard() is False
        assert deleted == []
        assert ctrl.state == IDLE

    def test_discard_abandoned_session_sends_no_backspace(self, rec):
        """已经放弃过的会话同理，只清理状态"""
        _, deleted = rec
        ctrl, focus = make_controller()
        ctrl.update("回车")
        focus.sig = (2, 2)
        ctrl.update("回车啊")            # 触发放弃
        assert ctrl.state == ABANDONED
        assert ctrl.discard() is False
        assert deleted == []

    def test_discard_failure_returns_false_and_resets(self, rec):
        """撤销失败（注入被拦截）时不阻塞命令执行，状态照常清理"""
        _, deleted = rec
        ctrl, _ = make_controller()
        ctrl.update("回车")
        with patch.object(direct_input.text_input, "send_backspace",
                          side_effect=SendTextError("UIPI 拦截")):
            assert ctrl.discard() is False
        assert deleted == []
        assert ctrl.state == IDLE

    def test_module_discard_delegates(self, monkeypatch, rec):
        _, deleted = rec
        ctrl, _ = make_controller()
        monkeypatch.setattr(direct_input, "_controller", ctrl)
        ctrl.update("回车")
        assert direct_input.discard() is True
        assert deleted == [2]


# ---------- 模块级单例 ----------
class TestModuleSingleton:
    def test_module_api_delegates_to_controller(self, monkeypatch, rec):
        typed, _ = rec
        ctrl, _ = make_controller()
        monkeypatch.setattr(direct_input, "_controller", ctrl)
        assert direct_input.get_controller() is ctrl
        assert direct_input.update("你好") is True
        assert direct_input.commit("你好啊") is True
        assert typed == ["你好", "啊"]
        assert ctrl.state == IDLE

    def test_module_reset(self, monkeypatch, rec):
        ctrl, _ = make_controller()
        monkeypatch.setattr(direct_input, "_controller", ctrl)
        direct_input.update("你好")
        direct_input.reset()
        assert ctrl.state == IDLE
