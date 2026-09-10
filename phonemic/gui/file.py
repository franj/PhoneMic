"""
文件接收：按 wire-protocol.md §9 / §9.2 的 file 帧把手机传来的文件落盘。

与 mouse.py / keyboard.py 同构：validate（纯函数、不碰磁盘、可单测）+
FileReceiver（状态机）。非法帧只记日志并返回错误信息，不把异常抛给事件循环。

落地规则（§9.2）：
    - 目录 ~/Downloads/PhoneMic/，不存在自动创建
    - 文件名沿用原名（剥离路径分隔符），重名按 name(1).ext 递增
    - data 逐块写 <final>.part 临时文件（调用方放线程执行），end 后改名
    - cancel / 连接断开 → 关句柄、删 .part、丢弃会话

帧示例（手机端原样发出）：
    {"type":"file", "a":"start",  "id":7, "name":"a.pdf", "size":1048576, "chunks":16}
    {"type":"file", "a":"data",   "id":7, "n":0, "chunk":<bin 256KB>}
    {"type":"file", "a":"end",    "id":7}
    {"type":"file", "a":"cancel", "id":7}
"""
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# a 取值白名单，与 wire-protocol.md §9 一一对应
VALID_ACTIONS = {'start', 'data', 'end', 'cancel'}

# 各动作要求的伴随字段
_REQUIRED_FIELDS = {
    'start': ('id', 'name', 'size', 'chunks'),
    'data': ('id', 'n', 'chunk'),
    'end': ('id',),
    'cancel': ('id',),
}

# 已带序号的重名文件：name(3).ext → 取 (3)；无扩展名时 name(3)
_SEQ_RE = re.compile(r'^(.*)\((\d+)\)(\.[^.]*)?$')

# 默认落地目录：~/Downloads/PhoneMic（§9.2，v1 写死）
DEFAULT_DEST_DIR = Path.home() / "Downloads" / "PhoneMic"


def validate_file_action(action: Any) -> Tuple[bool, str]:
    """校验 file 帧内容，返回 (是否合法, 错误信息)。

    只校验本模块关心的字段，帧里多带 type 等字段不影响。
    """
    if not isinstance(action, dict):
        return False, f"文件动作必须是对象，收到 {type(action).__name__}"

    a = action.get('a')
    if a not in VALID_ACTIONS:
        return False, f"未知文件动作: {a!r}，可选 {sorted(VALID_ACTIONS)}"

    for field in _REQUIRED_FIELDS[a]:
        if field not in action:
            return False, f"动作 '{a}' 缺少字段 '{field}'"
        value = action[field]
        # bool 是 int 的子类，显式排除
        if field in ('id', 'n', 'size', 'chunks'):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False, f"字段 '{field}' 必须是非负整数，收到 {value!r}"
        elif field == 'name':
            if not isinstance(value, str) or not value.strip():
                return False, f"字段 'name' 必须是非空字符串，收到 {value!r}"
        elif field == 'chunk':
            if not isinstance(value, (bytes, bytearray)):
                return False, f"字段 'chunk' 必须是二进制，收到 {type(value).__name__}"
    return True, ""


def _allocate_path(dest_dir: Path, name: str) -> Path:
    """在 dest_dir 下为 name 分配一个不冲突的路径（§9.2 重名规则）。

    name(3).pdf 已存在 → 新文件取 name(4).pdf（扫描取最大已用序号+1）；
    无冲突则原样返回。
    """
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate

    m = _SEQ_RE.match(name)
    if m:
        stem, _seq, suffix = m.group(1), m.group(2), m.group(3) or ""
        base = stem
    else:
        stem, suffix = os.path.splitext(name)
        base = stem

    max_seq = 0
    for p in dest_dir.iterdir():
        m2 = _SEQ_RE.match(p.name)
        if m2 and m2.group(1) == base and (m2.group(3) or "") == suffix:
            max_seq = max(max_seq, int(m2.group(2)))
    return dest_dir / f"{base}({max_seq + 1}){suffix}"


class FileReceiver:
    """file 帧接收状态机。按 id 路由会话，线程安全边界由调用方保证：
    handle() 每次在单一工作线程内完整执行（api.py 用 asyncio.to_thread），
    WS 的有序性 + 串行 await 保证帧序。

    on_done(final_path, name, size)：end 成功落盘后回调（托盘通知用）。
    """

    def __init__(self, dest_dir: Optional[Path] = None, on_done=None):
        self.dest_dir = Path(dest_dir) if dest_dir else DEFAULT_DEST_DIR
        self.on_done = on_done
        # id -> 会话状态
        self._sessions: Dict[int, Dict[str, Any]] = {}

    # ---- 对外入口 ----

    def handle(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """处理一条 file 帧。

        Returns:
            (ack帧, None)      —— data 帧正常，ack 由 api.py 下发给手机端
            (None, None)       —— start/end/cancel 正常处理，无需回帧
            (None, 错误信息)    —— 非法/协议错误，调用方回 error(malformed)
        """
        ok, err = validate_file_action(frame)
        if not ok:
            logger.error(f"文件帧非法: {frame.get('a')} - {err}")
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
            # 落盘失败等异常不能冒泡到事件循环；会话作废，等手机端 cancel 或重传
            logger.exception(f"处理文件帧失败: a={a} - {e}")
            fid = frame.get('id')
            if fid is not None:
                self._discard(fid)
            return None, f"接收失败: {e}"

    def abort_all(self) -> None:
        """连接断开时清理所有未完成会话（删 .part、丢状态）。"""
        for fid in list(self._sessions.keys()):
            self._discard(fid)

    # ---- 各子动作 ----

    def _on_start(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        if fid in self._sessions:
            # 同一连接内 start 后未 end/cancel 不允许开新传输（§9 单会话单文件）
            return None, f"传输 id={fid} 已在进行中，拒绝重复 start"
        if self._sessions:
            return None, f"已有传输在进行中（id={next(iter(self._sessions))}），v1 仅支持单文件"

        # 剥离路径分隔符，只取文件名末段
        name = Path(str(frame['name']).strip()).name
        if not name or name in ('.', '..'):
            return None, f"文件名非法: {frame['name']!r}"

        self.dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = _allocate_path(self.dest_dir, name)
        part_path = final_path.with_name(final_path.name + '.part')
        fh = open(part_path, 'wb')

        self._sessions[fid] = {
            'name': name,
            'size': frame['size'],
            'chunks': frame['chunks'],
            'received': 0,
            'final_path': final_path,
            'part_path': part_path,
            'fh': fh,
        }
        logger.info(f"文件传输开始: id={fid} name={name!r} size={frame['size']} -> {final_path}")
        return None, None

    def _on_data(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        sess = self._sessions.get(fid)
        if sess is None:
            return None, f"data 帧无匹配会话: id={fid}（未 start 或已结束）"

        chunk = frame['chunk']
        sess['fh'].write(chunk)
        sess['received'] += len(chunk)
        ack = {
            'type': 'ack',
            'ref': 'file',
            'id': fid,
            'n': frame['n'],
            'received': sess['received'],
        }
        return ack, None

    def _on_end(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        sess = self._sessions.pop(fid, None)
        if sess is None:
            return None, f"end 帧无匹配会话: id={fid}"

        fh = sess['fh']
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()

        # start 时分配的号码可能被中途占用，落盘前再查一次重名
        final_path = sess['final_path']
        if final_path.exists():
            final_path = _allocate_path(self.dest_dir, sess['name'])
        sess['part_path'].rename(final_path)

        logger.info(f"文件接收完成: {final_path}（{sess['received']} 字节）")
        if self.on_done:
            try:
                self.on_done(str(final_path), sess['name'], sess['received'])
            except Exception:
                logger.exception("on_done 回调失败")
        return None, None

    def _on_cancel(self, frame: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        fid = frame['id']
        if fid not in self._sessions:
            # 会话已结束/未开始：cancel 幂等成功，不回帧不报错
            logger.info(f"cancel 无匹配会话: id={fid}，忽略")
            return None, None
        self._discard(fid)
        logger.info(f"文件传输已取消: id={fid}")
        return None, None

    def _discard(self, fid: int) -> None:
        """关闭句柄、删临时文件、丢弃会话。cancel 与异常清理共用。"""
        sess = self._sessions.pop(fid, None)
        if sess is None:
            return
        try:
            sess['fh'].close()
        except Exception:
            pass
        try:
            sess['part_path'].unlink(missing_ok=True)
        except Exception:
            logger.exception(f"删除临时文件失败: {sess['part_path']}")
