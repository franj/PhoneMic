"""图片写入系统剪贴板（GUI 进程内调用，wire-protocol.md §9.1 photo 落地端）。

必须在 Qt 主线程调用（QApplication.clipboard() 需要 GUI 事件循环）。
Qt 的 setImage 会同时注册 CF_DIB / CF_DIBV5 / PNG 多种格式，粘贴进
微信 / Word / 画图 / 浏览器均可用，无需额外平台代码。
"""
import logging

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)


def copy_image(data: bytes) -> bool:
    """把图片字节（PNG/JPEG/BMP/GIF 等 Qt 可解码格式）写入系统剪贴板。

    Returns:
        True 写入成功；False 解码失败或剪贴板不可用（调用方提示用户）。
    """
    try:
        image = QImage.fromData(data)
    except Exception as e:   # 防御：fromData 理论上不抛，仍兜底
        logger.exception(f"QImage.fromData 异常: {e}")
        return False
    if image.isNull():
        logger.warning("剪贴板图片解码失败：字节不是可识别的图片格式")
        return False

    clipboard = QApplication.clipboard()
    if clipboard is None:
        logger.warning("系统剪贴板不可用")
        return False
    try:
        clipboard.setImage(image)
    except Exception as e:
        logger.exception(f"写入系统剪贴板失败: {e}")
        return False
    logger.info(f"图片已写入剪贴板: {image.width()}x{image.height()}")
    return True
