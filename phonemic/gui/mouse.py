"""
鼠标动作执行：按 wire-protocol.md §7 的 mouse 帧驱动 pyautogui。

与 keyboard.py 同构：先 validate（纯函数、不碰设备、可单测），
再 perform（非法输入只记日志并返回，不把异常抛给事件循环）。

帧示例（手机端原样发出，本模块只读其中字段）：
    {"type":"mouse", "a":"move",   "dx":12, "dy":-3}
    {"type":"mouse", "a":"click",  "btn":"left"}
    {"type":"mouse", "a":"double", "btn":"left"}
    {"type":"mouse", "a":"down",   "btn":"left"}
    {"type":"mouse", "a":"up",     "btn":"left"}
    {"type":"mouse", "a":"wheel",  "delta":-120}
"""
import logging
from typing import Any, Dict, Tuple

import pyautogui

logger = logging.getLogger(__name__)

# 关闭 pyautogui 的左上角保险：手机遥控鼠标时 (0,0) 是合法目标位置。
# 不关的话，光标一旦移到左上角，后续每次 pyautogui 调用都会在 failSafeCheck
# 里抛异常，鼠标彻底卡死，只能手动挪开物理鼠标才能恢复——遥控场景下用户
# 未必在电脑前。要停止遥控，走手机端断开连接。
pyautogui.FAILSAFE = False

# a 取值白名单，与 wire-protocol.md §7 一一对应
VALID_ACTIONS = {'move', 'click', 'double', 'down', 'up', 'wheel'}
VALID_BUTTONS = {'left', 'right', 'middle'}

# 帧统计回调（调试监视器 phonemic/gui/mouse_debug.py 用）。
# 走回调而非直接 import，是为了让本模块保持零 Qt 依赖——test_mouse.py 是纯单测，
# 不该因为接了个临时调试组件就被拖进 PySide6。
_stats_hook = None


def set_stats_hook(fn) -> None:
    """注册 move 帧统计回调 fn(dx, dy)；传 None 取消。"""
    global _stats_hook
    _stats_hook = fn

# 各动作要求的伴随字段
_REQUIRED_FIELDS = {
    'move': ('dx', 'dy'),
    'click': ('btn',),
    'double': ('btn',),
    'down': ('btn',),
    'up': ('btn',),
    'wheel': ('delta',),
}


def validate_mouse_action(action: Any) -> Tuple[bool, str]:
    """校验 mouse 帧内容，返回 (是否合法, 错误信息)。

    只校验本模块关心的字段，帧里多带 type 等字段不影响。
    """
    if not isinstance(action, dict):
        return False, f"鼠标动作必须是对象，收到 {type(action).__name__}"

    a = action.get('a')
    if a not in VALID_ACTIONS:
        return False, f"未知鼠标动作: {a!r}，可选 {sorted(VALID_ACTIONS)}"

    for field in _REQUIRED_FIELDS[a]:
        if field not in action:
            return False, f"动作 '{a}' 缺少字段 '{field}'"
        value = action[field]
        if field == 'btn':
            if value not in VALID_BUTTONS:
                return False, f"未知鼠标键: {value!r}，可选 {sorted(VALID_BUTTONS)}"
        # bool 是 int 的子类，显式排除，避免 True 当 1 像素用
        elif isinstance(value, bool) or not isinstance(value, int):
            return False, f"字段 '{field}' 必须是整数，收到 {value!r}"
    return True, ""


def perform_mouse(action: Dict[str, Any]) -> None:
    """执行鼠标动作。非法输入只记 error 日志并返回。"""
    ok, err = validate_mouse_action(action)
    if not ok:
        logger.error(f"鼠标动作非法: {action} - {err}")
        return

    a = action['a']
    try:
        # 所有动作都带 _pause=False：pyautogui 默认每次调用后 sleep 0.1s（PAUSE），
        # 实测 click 单次 109ms。滚轮长按连发会变成 240ms/格，点按也有明显粘滞感。
        if a == 'move':
            pyautogui.moveRel(action['dx'], action['dy'], _pause=False)
            if _stats_hook:
                _stats_hook(action['dx'], action['dy'])
        elif a == 'click':
            pyautogui.click(button=action['btn'], _pause=False)
        elif a == 'double':
            pyautogui.doubleClick(button=action['btn'], _pause=False)
        elif a == 'down':
            pyautogui.mouseDown(button=action['btn'], _pause=False)
        elif a == 'up':
            pyautogui.mouseUp(button=action['btn'], _pause=False)
        elif a == 'wheel':
            # 协议约定 delta 正下负上；pyautogui.scroll 正数向上，故取反
            pyautogui.scroll(-action['delta'], _pause=False)
    except Exception as e:
        # 单个动作失败不能冒泡到事件消费循环，否则会中断后续事件
        logger.exception(f"执行鼠标动作失败: {a} - {e}")
