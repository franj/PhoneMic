"""
图片到剪贴板：按 wire-protocol.md §9 / §9.1 的 photo 帧在内存重组图片字节，
end 后整体交给 on_done 回调（GUI 进程写系统剪贴板，不落盘）。

与 mouse.py / keyboard.py / file.py 同构：validate（纯函数、不碰磁盘、可单测）+
PhotoReceiver（状态机）。非法帧只记日志并返回错误信息，不把异常抛给事件循环。

photo 与 file 线格式完全相同，唯一区别是落地方式：
    file  → 磁盘（file.py）
    photo → 剪贴板（本模块），name 字段对剪贴板无意义（§9.1），可选
内存重组天然要求整图驻留内存——v1 不做大小上限（与 file 一致，§13 #3 未约束），
但会对"收到的字节数超过 start 声明的 size"这类明显协议错误直接报错拒绝。

帧示例（手机端原样发出）：
    {"type":"photo", "a":"start",  "id":7, "name":"a.png", "size":1048576, "chunks":16}
    {"type":"photo", "a":"data",   "id":7, "n":0, "chunk":<bin 1MB>}
    {"type":"photo", "a":"end",    "id":7}
    {"type":"photo", "a":"cancel", "id":7}
"""
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# a 取值白名单，与 wire-protocol.md §9 一一对应（photo 与 file 共享同一套子协议）
VALID_ACTIONS = {'start', 'data', 'end', 'cancel'}

# 各动作要求的伴随字段。photo 的 name 对剪贴板无意义（§9.1），start 不强制要求
_REQUIRED_FIELDS = {
    'start': ('id', 'size', 'chunks'),
    'data': ('id', 'n', 'chunk'),
    'end': ('id',),
    'cancel': ('id',),
}


def validate_photo_action(action: Any) -> Tuple[bool, str]:
    """校验 photo 帧内容，返回 (是否合法, 错误信息)。

    只校验本模块关心的字段，帧里多带 type/name 等字段不影响。
    """
    if not isinstance(action, dict):
        return False, f"图片动作必须是对象，收到 {type(action).__name__}"

    a = action.get('a')
    if a not in VALID_ACTIONS:
        return False, f"未知图片动作: {a!r}，可选 {sorted(VALID_ACTIONS)}"

    for field in _REQUIRED_FIELDS[a]:
        if field not in action:
            return False, f"动作 '{a}' 缺少字段 '{field}'"
        value = action[field]
        # bool 是 int 的子类，显式排除
        if field in ('id', 'n', 'size', 'chunks'):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False, f"字段 '{field}' 必须是非负整数，收到 {value!r}"
        elif field == 'chunk':
            if not isinstance(value, (bytes, bytearray)):
                return False, f"字段 'chunk' 必须是二进制，收到 {type(value).__name__}"
    return True, ""


class PhotoReceiver:
    """photo 帧接收状态机（内存重组，不落盘）。

    按 id 路由会话，线程安全边界由调用方保证：handle() 每次在单一工作线程内
    完整执行（api.py 用 asyncio.to_thread），WS 的有序性 + 串行 await 保证帧序。

    on_done(data, name, size)：end 收齐后回调（data 为重组后的完整图片字节）。
    name 可能为 None（协议允许省略）。
    """

    def __init__(self, on_done=None):
        self.on_done = on_done
        # id -> 会话状态
        self._sessions: Dict[int, Dict[str, Any]] = {}

    # ---- 对外入口 ----

    def handle(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """处理一条 photo 帧。

        Returns:
            (ack帧, None)      —— end 重组成功，ack(a:"end") 由 api.py 下发给手机端
            (None, None)       —— start/data/cancel 正常处理，无需回帧
            (None, 错误信息)    —— 非法/协议错误/没收齐，调用方回 error(malformed)
        """
        ok, err = validate_photo_action(frame)
        if not ok:
            logger.error(f"图片帧非法: {frame.get('a')} - {err}")
            return None, err

        a = frame['a']
        try:
            if a == 'start':
                return self._on_start(frame)
            if a == 'data':
                return self._on_data(frame)
            if a == 'end':
                return self._on_end(frame)
            return self._on_cancel(frame)
        except Exception as e:
            # 内存异常等不能冒泡到事件循环；会话作废，等手机端 cancel 或重传
            logger.exception(f"处理图片帧失败: a={a} - {e}")
            fid = frame.get('id')
            if fid is not None:
                self._discard(fid)
            return None, f"接收失败: {e}"

    def abort_all(self) -> None:
        """连接断开时清理所有未完成会话（丢内存 buffer、丢状态）。"""
        for fid in list(self._sessions.keys()):
            self._discard(fid)

    # ---- 各子动作 ----

    def _on_start(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        if fid in self._sessions:
            # 同一连接内 start 后未 end/cancel 不允许开新传输（§9 单会话单文件）
            return None, f"传输 id={fid} 已在进行中，拒绝重复 start"
        if self._sessions:
            return None, f"已有图片传输在进行中（id={next(iter(self._sessions))}），v1 仅支持单文件"

        # name 对剪贴板无意义（§9.1），仅用于日志与托盘提示，可缺省
        name = frame.get('name')
        if name is not None:
            name = str(name).strip()
            if name in ('.', '..'):
                return None, f"文件名非法: {frame['name']!r}"

        self._sessions[fid] = {
            'name': name or None,
            'size': frame['size'],
            'chunks': frame['chunks'],
            'received': 0,
            'buf': bytearray(),
        }
        logger.info(f"图片传输开始: id={fid} name={name!r} size={frame['size']}")
        return None, None

    def _on_data(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        sess = self._sessions.get(fid)
        if sess is None:
            return None, f"data 帧无匹配会话: id={fid}（未 start 或已结束）"

        chunk = frame['chunk']
        sess['received'] += len(chunk)
        # 已收字节超过 start 声明的 size → 协议错误（断块重复/乱序），拒绝继续
        if sess['received'] > sess['size']:
            self._discard(fid)
            return None, (f"data 帧累计字节超过声明的 size: "
                          f"id={fid} received={sess['received']} size={sess['size']}")
        sess['buf'].extend(chunk)
        # 不再逐块回 ack（协议 §9），只在 end 重组成功后回一次确认
        return None, None

    def _on_end(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        sess = self._sessions.pop(fid, None)
        if sess is None:
            return None, f"end 帧无匹配会话: id={fid}"

        # 收齐校验：字节数对不上说明中途丢块，写进剪贴板会是损坏图片
        if sess['received'] != sess['size']:
            return None, (f"接收不完整: id={fid} received={sess['received']} "
                          f"size={sess['size']}")

        data = bytes(sess['buf'])
        logger.info(f"图片接收完成: {sess['name'] or '(unnamed)'}（{sess['received']} 字节）")
        if self.on_done:
            try:
                self.on_done(data, sess['name'], sess['received'])
            except Exception:
                logger.exception("on_done 回调失败")
        return {
            'type': 'ack',
            'ref': 'photo',
            'id': fid,
            'a': 'end',
            'received': sess['received'],
        }, None

    def _on_cancel(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        if fid not in self._sessions:
            # 会话已结束/未开始：cancel 幂等成功，不回帧不报错
            logger.info(f"cancel 无匹配会话: id={fid}，忽略")
            return None, None
        self._discard(fid)
        logger.info(f"图片传输已取消: id={fid}")
        return None, None

    def _discard(self, fid: int) -> None:
        """丢弃会话（释放内存 buffer）。cancel 与异常清理共用。"""
        self._sessions.pop(fid, None)
