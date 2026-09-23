"""图片到剪贴板的接收 sink：在内存重组图片字节，收齐后整体交给 on_done。

形态（docs/http-upload-design.md §7）：从「分块喂字节的状态机」改为
「整段写入 + 收尾」，与 gui/file.py 的 FileSink 同构。photo 与 file 的唯一区别
是落地方式：file → 磁盘，photo → 剪贴板（GUI 进程执行，不落盘）。

图片必须整图驻留内存才能写剪贴板，所以 photo 有**强制大小上限**
（§5.7，在 upload_begin 阶段就拒，不是收到一半才失败）；上限常量在
``server/upload.py``，因为「拒在第 ① 步」是会话表的职责。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PhotoSink:
    """一张图片的内存重组 sink：write* → finish（→ on_done）/ abort。

    :param size:    声明的明文总字节数
    :param name:    原文件名，对剪贴板无意义（§9.8），仅用于日志与托盘提示
    :param on_done: 收齐后的回调 ``(data, name, size)``
    """

    def __init__(self, size: int, name: Optional[str] = None, on_done=None):
        self.size = int(size)
        self.name = name or None
        # 与 FileSink 对齐的字段：剪贴板没有文件名概念，这里只用于结果提示
        self.saved = name or ""
        self.on_done = on_done
        self._buf = bytearray()

    # ---- 写入 ----

    def write(self, data: bytes) -> None:
        # 上限在会话层已经把住（received 不可能越过 size）；这里是纵深防御
        if len(self._buf) + len(data) > self.size:
            raise ValueError(
                f"图片累计字节超过声明的 size: {len(self._buf) + len(data)} > {self.size}")
        self._buf.extend(data)

    # ---- 收尾 ----

    def finish(self) -> bytes:
        """交出重组后的完整图片字节（同时触发 on_done）。"""
        data = bytes(self._buf)
        logger.info(f"图片接收完成: {self.name or '(unnamed)'}（{len(data)} 字节）")
        if self.on_done:
            try:
                self.on_done(data, self.name, len(data))
            except Exception:
                logger.exception("on_done 回调失败")
        return data

    def abort(self) -> None:
        """丢弃内存缓冲。**幂等**。"""
        self._buf = bytearray()
