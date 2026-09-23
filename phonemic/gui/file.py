"""文件落盘 sink：把上传会话收到的明文字节写到 ~/Downloads/PhoneMic/。

形态（docs/http-upload-design.md §7）：从「分块喂字节的状态机」改为
「整段写入 + 收尾」——上传侧（server/upload.py）每收下一片就 ``write()`` 一次，
收齐后 ``finish()`` 改名落盘，任何失败走 ``abort()``。帧的解析、校验、序号判定
全部搬去了 upload.py 的会话状态机，这里只管磁盘。

落地规则（wire-protocol.md §9.9，沿用）：
    - 目录 ~/Downloads/PhoneMic/，不存在自动创建
    - 文件名沿用原名（剥离路径分隔符），重名按 name(1).ext 递增
    - 数据先写 <final>.part，finish() 时改名；abort() 关句柄并删掉
    - finish() 前再查一次重名（start 时分配的号码可能被中途占用）

⚠️ 所有方法都是**同步的**、要操作 15MB 量级的数据，调用方必须放进线程执行
（``asyncio.to_thread``），别在事件循环里直接调。
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 已带序号的重名文件：name(3).ext → 取 (3)；无扩展名时 name(3)
_SEQ_RE = re.compile(r'^(.*)\((\d+)\)(\.[^.]*)?$')

# 默认落地目录：~/Downloads/PhoneMic（§9.9，v1 写死）
DEFAULT_DEST_DIR = Path.home() / "Downloads" / "PhoneMic"


def sanitize_name(name: object) -> str:
    """剥离路径分隔符，只取文件名末段。

    Raises:
        ValueError: 名字为空 / 只剩 '.' / '..'
    """
    cleaned = Path(str(name).strip()).name
    if not cleaned or cleaned in ('.', '..'):
        raise ValueError(f"文件名非法: {name!r}")
    return cleaned


def _allocate_path(dest_dir: Path, name: str) -> Path:
    """在 dest_dir 下为 name 分配一个不冲突的路径（§9.9 重名规则）。

    name(3).pdf 已存在 → 新文件取 name(4).pdf（扫描取最大已用序号+1）；
    无冲突则原样返回。

    ⚠️ 还要看 ``<name>.part``：`.part` 是**在途**的占位，同名会话若只看最终名
    会双双选到同一个 ``a.pdf``，于是两边写到同一个 ``a.pdf.part`` 上（后开的
    那个 open('wb') 直接把前一个截断）。把 `.part` 也算作占用即可绕开。
    """
    def _taken(p: Path) -> bool:
        return p.exists() or p.with_name(p.name + ".part").exists()

    candidate = dest_dir / name
    if not _taken(candidate):
        return candidate

    m = _SEQ_RE.match(name)
    if m:
        base, suffix = m.group(1), m.group(3) or ""
    else:
        base, suffix = os.path.splitext(name)

    max_seq = 0
    for p in dest_dir.iterdir():
        m2 = _SEQ_RE.match(p.name)
        if m2 and m2.group(1) == base and (m2.group(3) or "") == suffix:
            max_seq = max(max_seq, int(m2.group(2)))
    # 递增时同样要跳过被 .part 占住的号码
    n = max_seq + 1
    while _taken(dest_dir / f"{base}({n}){suffix}"):
        n += 1
    return dest_dir / f"{base}({n}){suffix}"


class FileSink:
    """一个文件的落盘 sink：构造即建 .part，write* → finish / abort。

    :param name:      客户端声明的原文件名（会被剥离路径分隔符）
    :param dest_dir:  落地目录，默认 ``~/Downloads/PhoneMic``
    :param on_done:   落盘成功后的回调 ``(path, name, size)``（托盘通知用）

    ``saved`` 是服务端分配好的最终文件名（重名已改号），在 ``upload_ready``
    里下发给客户端显示。``finish()`` 时会再查一次重名，所以它有可能被改。
    """

    def __init__(self, name: object, dest_dir: Optional[Path] = None, on_done=None):
        self.dest_dir = Path(dest_dir) if dest_dir else DEFAULT_DEST_DIR
        self.on_done = on_done
        self.name = sanitize_name(name)
        self.written = 0

        self.dest_dir.mkdir(parents=True, exist_ok=True)
        self._final_path = _allocate_path(self.dest_dir, self.name)
        self._part_path = self._final_path.with_name(self._final_path.name + ".part")
        self._fh = open(self._part_path, "wb")
        self.saved = self._final_path.name
        self._closed = False

    # ---- 写入 ----

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("sink 已关闭")
        self._fh.write(data)
        self.written += len(data)

    # ---- 收尾 ----

    def finish(self) -> str:
        """flush + fsync + 关句柄 + 改名落盘，返回最终路径。"""
        if self._closed:
            raise RuntimeError("sink 已关闭")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._closed = True

        # 分配号码时可能被中途占用，落盘前再查一次重名
        final_path = self._final_path
        if final_path.exists():
            final_path = _allocate_path(self.dest_dir, self.name)
        self._part_path.rename(final_path)
        self._final_path = final_path
        self.saved = final_path.name

        logger.info(f"文件接收完成: {final_path}（{self.written} 字节）")
        if self.on_done:
            try:
                self.on_done(str(final_path), self.name, self.written)
            except Exception:
                logger.exception("on_done 回调失败")
        return str(final_path)

    def abort(self) -> None:
        """关句柄、删 .part。**幂等**：取消与异常清理会重复调用它。"""
        if not self._closed:
            try:
                self._fh.close()
            except Exception:
                pass
            self._closed = True
        try:
            self._part_path.unlink(missing_ok=True)
        except Exception:
            logger.exception(f"删除临时文件失败: {self._part_path}")
