"""
隧道服务提供者抽象基类。
定义统一的隧道管理接口，各隧道服务（Cloudflare、cpolar 等）实现此接口。
"""

from abc import ABC, abstractmethod
from typing import Optional, Callable


class TunnelProvider(ABC):
    """隧道服务提供者抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """显示名称。"""

    @abstractmethod
    def start(self, port: int) -> None:
        """启动隧道，转发流量到本地指定端口。"""

    @abstractmethod
    def stop(self) -> None:
        """停止隧道。"""

    @abstractmethod
    def get_url(self) -> Optional[str]:
        """获取当前隧道公网 URL，未就绪时返回 None。"""

    @abstractmethod
    def is_running(self) -> bool:
        """隧道进程是否正在运行。"""

    @abstractmethod
    def is_available(self) -> bool:
        """检测所需二进制文件是否已安装。"""
