"""
phonemic/gui/mouse.py 测试。

只覆盖纯逻辑与调用参数：validate 是纯函数，perform 用 mock 断言它把帧
翻译成了正确的 pyautogui 调用，不真的动鼠标。
"""
from unittest import mock

import pytest

from phonemic.gui import mouse


class TestValidateMouseAction:
    """校验规则与 wire-protocol.md §7 一致。"""

    @pytest.mark.parametrize("action", [
        {"a": "move", "dx": 12, "dy": -3},
        {"a": "click", "btn": "left"},
        {"a": "double", "btn": "right"},
        {"a": "down", "btn": "middle"},
        {"a": "up", "btn": "left"},
        {"a": "wheel", "delta": -120},
    ])
    def test_valid_actions(self, action):
        assert mouse.validate_mouse_action(action)[0] is True

    def test_frame_with_type_field_still_valid(self):
        """帧里带 type 不影响校验（服务端转发的是整帧）。"""
        ok, _ = mouse.validate_mouse_action({"type": "mouse", "a": "click", "btn": "left"})
        assert ok is True

    def test_non_dict_rejected(self):
        ok, err = mouse.validate_mouse_action("click")
        assert ok is False
        assert "对象" in err

    def test_unknown_action_rejected(self):
        ok, err = mouse.validate_mouse_action({"a": "triple"})
        assert ok is False
        assert "未知鼠标动作" in err

    @pytest.mark.parametrize("action", [
        {"a": "move", "dx": 1},               # 缺 dy
        {"a": "click"},                       # 缺 btn
        {"a": "wheel"},                       # 缺 delta
    ])
    def test_missing_field_rejected(self, action):
        ok, err = mouse.validate_mouse_action(action)
        assert ok is False
        assert "缺少字段" in err

    def test_unknown_button_rejected(self):
        ok, err = mouse.validate_mouse_action({"a": "click", "btn": "fourth"})
        assert ok is False
        assert "未知鼠标键" in err

    @pytest.mark.parametrize("action", [
        {"a": "move", "dx": 1.5, "dy": 0},    # 协议要求整数
        {"a": "move", "dx": "12", "dy": 0},
        {"a": "wheel", "delta": True},        # bool 是 int 子类，须排除
    ])
    def test_non_integer_rejected(self, action):
        ok, err = mouse.validate_mouse_action(action)
        assert ok is False
        assert "整数" in err


class TestPerformMouse:
    """帧 → pyautogui 调用的翻译。"""

    def test_move_without_pause(self):
        """move 帧约 60/s，必须绕开 pyautogui 默认 0.1s 的调用后暂停。"""
        with mock.patch.object(mouse.pyautogui, "moveRel") as m:
            mouse.perform_mouse({"a": "move", "dx": 12, "dy": -3})
        m.assert_called_once_with(12, -3, _pause=False)

    @pytest.mark.parametrize("action,func,expected", [
        ({"a": "click", "btn": "left"}, "click", {"button": "left", "_pause": False}),
        ({"a": "click", "btn": "right"}, "click", {"button": "right", "_pause": False}),
        ({"a": "double", "btn": "left"}, "doubleClick", {"button": "left", "_pause": False}),
        ({"a": "down", "btn": "left"}, "mouseDown", {"button": "left", "_pause": False}),
        ({"a": "up", "btn": "left"}, "mouseUp", {"button": "left", "_pause": False}),
    ])
    def test_button_actions(self, action, func, expected):
        with mock.patch.object(mouse.pyautogui, func) as m:
            mouse.perform_mouse(action)
        m.assert_called_once_with(**expected)

    @pytest.mark.parametrize("delta,expected_clicks", [(-120, 120), (120, -120), (0, 0)])
    def test_wheel_inverts_delta(self, delta, expected_clicks):
        """协议 delta 正下负上，pyautogui.scroll 正数向上，故取反。"""
        with mock.patch.object(mouse.pyautogui, "scroll") as m:
            mouse.perform_mouse({"a": "wheel", "delta": delta})
        m.assert_called_once_with(expected_clicks, _pause=False)

    def test_invalid_action_does_nothing(self):
        """非法帧只记日志，不产生任何 pyautogui 调用。"""
        with mock.patch.object(mouse.pyautogui, "click") as click, \
             mock.patch.object(mouse.pyautogui, "moveRel") as move:
            mouse.perform_mouse({"a": "click", "btn": "nonexistent"})
        click.assert_not_called()
        move.assert_not_called()

    def test_exception_does_not_propagate(self):
        """单次动作失败不能冒泡到事件消费循环。"""
        with mock.patch.object(mouse.pyautogui, "click", side_effect=RuntimeError("boom")):
            mouse.perform_mouse({"a": "click", "btn": "left"})  # 不应抛出

